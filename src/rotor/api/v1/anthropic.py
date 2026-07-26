import time
import uuid
import logging
import json
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, Request, Response, status, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import async_session_maker, get_db
from rotor.core.deps import get_current_token, get_available_channels
from rotor.application_settings import application_settings
from rotor.schemas.request import (
    AnthropicMessageRequest,
    AnthropicCountTokensRequest,
    AnthropicMessageResponse,
    AnthropicUsage,
    AnthropicTextBlock,
    ErrorResponse,
    ChatCompletionRequest,
    ChatMessage,
    Function,
    Role,
    Tool,
    ToolCall,
)
from rotor.adapters.factory import AdapterFactory
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import routing_engine
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
from rotor.conversations.store import ConversationHandle
from rotor.api.v1.chat import conversation_store
from rotor.models.channel import Channel
from rotor.core.exceptions import (
    ChannelException,
    classify_error_status,
    format_error_message,
    upstream_error_payload,
)
from httpx import AsyncClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

router = APIRouter()
accounting_service = AccountingService()


def anthropic_to_openai_request(anthropic_request: AnthropicMessageRequest) -> ChatCompletionRequest:
    """Convert Anthropic request to internal OpenAI format."""
    messages = []
    # Add system message if present (system may be a string or a list of
    # content blocks, e.g. [{"type": "text", "text": "...", "cache_control": {...}}])
    if anthropic_request.system:
        system = anthropic_request.system
        if isinstance(system, list):
            parts = [
                block.get("text", "")
                for block in system
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            system = "\n".join(parts) or None
        if system:
            messages.append(ChatMessage(role=Role.SYSTEM, content=system))

    # Convert Anthropic messages to the internal OpenAI-compatible format.
    for msg in anthropic_request.messages:
        if msg.role == "user":
            content = msg.content
            if isinstance(content, str):
                messages.append(ChatMessage(role=Role.USER, content=content))
                continue

            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    if text_parts:
                        messages.append(ChatMessage(
                            role=Role.USER,
                            content="".join(text_parts),
                        ))
                        text_parts = []
                    messages.append(ChatMessage(
                        role=Role.TOOL,
                        content=_anthropic_block_content_to_text(block.get("content")),
                        tool_call_id=block.get("tool_use_id"),
                    ))
            if text_parts:
                messages.append(ChatMessage(
                    role=Role.USER,
                    content="".join(text_parts),
                ))
        elif msg.role == "assistant":
            content = msg.content
            if isinstance(content, str):
                messages.append(ChatMessage(role=Role.ASSISTANT, content=content))
                continue

            text_parts = []
            tool_calls = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block.get("id", ""),
                        type="function",
                        function={
                            "name": block.get("name", ""),
                            "arguments": json.dumps(
                                block.get("input", {}),
                                ensure_ascii=False,
                            ),
                        },
                    ))
            messages.append(ChatMessage(
                role=Role.ASSISTANT,
                content="".join(text_parts) or None,
                tool_calls=tool_calls or None,
            ))

    tools = None
    if anthropic_request.tools:
        tools = [
            Tool(function=Function(
                name=tool.name,
                description=tool.description,
                parameters=tool.input_schema,
            ))
            for tool in anthropic_request.tools
        ]

    return ChatCompletionRequest(
        model=anthropic_request.model,
        messages=messages,
        max_tokens=anthropic_request.max_tokens,
        temperature=anthropic_request.temperature,
        top_p=anthropic_request.top_p,
        stop=anthropic_request.stop_sequences,
        stream=anthropic_request.stream or False,
        tools=tools,
        tool_choice=_anthropic_tool_choice_to_openai(anthropic_request.tool_choice),
        anthropic_payload=anthropic_request.model_dump(exclude_none=True),
    )


def _forwarded_anthropic_headers(http_request: Request) -> dict[str, str]:
    return {
        name: value
        for name in ("anthropic-beta", "anthropic-version")
        if (value := http_request.headers.get(name))
    }


def _anthropic_block_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return text or json.dumps(content, ensure_ascii=False)


def _anthropic_tool_choice_to_openai(tool_choice):
    if not tool_choice:
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {
            "type": "function",
            "function": {"name": tool_choice["name"]},
        }
    return None


def openai_to_anthropic_response(openai_response: dict, model: str) -> dict:
    """Convert OpenAI format response to Anthropic format."""
    # Extract content and tool calls
    choices = openai_response.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content", "")
    tool_calls = message.get("tool_calls") or []

    # Build content blocks
    content_blocks = []
    if content:
        content_blocks.append({
            "type": "text",
            "text": content
        })

    # Handle tool calls
    for tool_call in tool_calls:
        function = tool_call.get("function", {})
        arguments = function.get("arguments", "{}")
        try:
            tool_input = (
                arguments
                if isinstance(arguments, dict)
                else json.loads(arguments or "{}")
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid tool arguments from upstream: %r", arguments)
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id", ""),
            "name": function.get("name", ""),
            "input": tool_input,
        })

    # Map finish reason
    finish_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "stop_sequence",
    }
    stop_reason = finish_reason_map.get(choice.get("finish_reason", "stop"), "end_turn")

    # Convert usage
    usage = openai_response.get("usage") or {}
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0)
    }

    return {
        "id": openai_response.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "content": content_blocks if content_blocks else [{"type": "text", "text": ""}],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage
    }


@router.post("/messages/count_tokens", response_model=None)
async def count_message_tokens(
    request: AnthropicCountTokensRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Count Anthropic input tokens, proxying native channels when possible."""
    channels = await get_available_channels(request.model, token, db)
    native_channels = [
        channel
        for channel in channels
        if str(channel.protocol or "").lower() in {"anthropic", "anthropic_messages"}
    ]
    if native_channels:
        async with AsyncClient(timeout=120.0) as http_client:
            adapter = AdapterFactory.create_adapter(native_channels[0], http_client)
            if getattr(adapter, "native_anthropic", False):
                upstream = await adapter.count_tokens(
                    request.provider_payload(),
                    _forwarded_anthropic_headers(http_request),
                )
                return Response(
                    content=upstream.content,
                    status_code=upstream.status_code,
                    media_type="application/json",
                )

    # Cross-protocol providers do not expose Anthropic's tokenizer. Return a
    # deterministic conservative estimate so Claude Code can manage context.
    serialized = json.dumps(
        request.provider_payload(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return {"input_tokens": max(1, (len(serialized) + 3) // 4)}


class OpenAIToAnthropicStreamConverter:
    """Stateful conversion from OpenAI chunks to valid Anthropic SSE events."""

    def __init__(self):
        self.next_block_index = 0
        self.text_block_index = None
        self.tool_blocks = {}
        self.open_blocks = set()
        self.finished = False
        self.pending_finish_reason = None

    def feed(self, openai_chunk: dict) -> list[dict]:
        choices = openai_chunk.get("choices", [])
        if not choices or self.finished:
            return []

        events = []
        choice = choices[0]
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if content:
            if self.text_block_index is None:
                self.text_block_index = self._allocate_block()
                events.append({
                    "type": "content_block_start",
                    "index": self.text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            events.append({
                "type": "content_block_delta",
                "index": self.text_block_index,
                "delta": {"type": "text_delta", "text": content},
            })

        for tool_call in delta.get("tool_calls") or []:
            tool_index = tool_call.get("index", 0)
            function = tool_call.get("function") or {}
            if tool_index not in self.tool_blocks:
                block_index = self._allocate_block()
                self.tool_blocks[tool_index] = block_index
                events.append({
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": {},
                    },
                })
            arguments = function.get("arguments")
            if arguments:
                events.append({
                    "type": "content_block_delta",
                    "index": self.tool_blocks[tool_index],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": arguments,
                    },
                })

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            # OpenAI-compatible streams commonly send a usage-only chunk
            # after the chunk carrying finish_reason. Delay Anthropic's
            # terminal events until the upstream stream is exhausted.
            self.pending_finish_reason = finish_reason
        return events

    def finish(self, finish_reason: Optional[str] = None, usage: Optional[dict] = None) -> list[dict]:
        if self.finished:
            return []
        self.finished = True
        events = [
            {"type": "content_block_stop", "index": index}
            for index in sorted(self.open_blocks)
        ]
        finish_reason = finish_reason or self.pending_finish_reason or "stop"
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "stop_sequence",
        }
        usage = usage or {}
        events.append({
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason_map.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {
                "output_tokens": usage.get("completion_tokens", 0),
            },
        })
        events.append({"type": "message_stop"})
        return events

    def _allocate_block(self) -> int:
        index = self.next_block_index
        self.next_block_index += 1
        self.open_blocks.add(index)
        return index


@router.post("/messages", response_model=None)
async def messages(
    request: AnthropicMessageRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token = Depends(get_current_token),
):
    """
    Anthropic-compatible messages endpoint.

    Supports:
    - Multiple providers with automatic failover
    - Streaming and non-streaming responses
    - Token usage tracking
    - Request logging
    """
    # Convert Anthropic request to internal format
    internal_request = anthropic_to_openai_request(request)
    internal_request.anthropic_headers = _forwarded_anthropic_headers(http_request)

    # Get available channels for the model
    channels = await get_available_channels(request.model, token, db)

    # Record start time
    start_time = time.time()
    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    conversation_id = (
        http_request.headers.get("X-Conversation-Id")
        or (request.metadata or {}).get("user_id")
        or f"conv_{uuid.uuid4().hex[:24]}"
    )
    client_ip = http_request.client.host if http_request.client else "unknown"
    affinity_used = application_settings.get().routing.affinity_enabled
    required_capabilities = {"stream"} if request.stream else set()
    routing_decision = routing_engine.route(
        channels,
        model=request.model,
        token=token,
        request_protocol="anthropic_messages",
        required_capabilities=required_capabilities,
        affinity_key=conversation_id if affinity_used else None,
    )
    accounting_service.record_routing_decision(
        db,
        request_id=request_id,
        token=token,
        model=request.model,
        request_protocol="anthropic_messages",
        decision=routing_decision,
        required_capabilities=required_capabilities,
        affinity_used=affinity_used,
        features={
            "stream": bool(request.stream),
            "message_count": len(request.messages),
            "has_tools": bool(request.tools),
        },
    )
    await db.commit()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    last_error = None

    conversation_handle = await conversation_store.start(
        db,
        conversation_id=conversation_id,
        request_id=request_id,
        token=token,
        request=internal_request,
        protocol="anthropic_messages",
    )
    try:
        for attempt, channel in enumerate(routing_decision.candidates):
            try:
                routing_engine.begin_attempt(request.model, channel)
                # Create adapter
                adapter = AdapterFactory.create_adapter(channel, http_client)
                await conversation_store.append_routing(conversation_handle, channel)

                # Make request
                if request.stream:
                    response = await adapter.make_request(internal_request)
                    native_anthropic = bool(
                        getattr(adapter, "native_anthropic", False)
                        and internal_request.anthropic_payload is not None
                    )
                    result = await _handle_streaming_request(
                        request, internal_request, adapter, channel, token, db,
                        start_time, http_request, http_client, request_id,
                        conversation_id, response, conversation_handle,
                        native_anthropic_stream=native_anthropic,
                    )
                    set_routing_headers(result, channel, request.model, attempt > 0)
                    return result
                else:
                    set_routing_headers(
                        api_response, channel, request.model, attempt > 0
                    )
                    return await _handle_non_streaming_request(
                        request, internal_request, adapter, channel, token, db, start_time,
                        http_request, request_id, conversation_id, conversation_handle,
                    )

            except (HTTPStatusError, RequestError) as e:
                last_error = e
                logger.warning(f"Channel {channel.name} failed: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="anthropic_messages",
                    token=token,
                    channel=channel,
                    model=request.model,
                    error_code=type(e).__name__,
                    error_message=format_error_message(e),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                    provider_response=upstream_error_payload(e),
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle, type(e).__name__, format_error_message(e)
                    )
                await db.commit()
                if should_fallback(e):
                    routing_engine.mark_unavailable(
                        request.model,
                        channel,
                        retry_after_seconds(e),
                    )
                    continue
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                raise

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error with channel {channel.name}: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="anthropic_messages",
                    token=token,
                    channel=channel,
                    model=request.model,
                    error_code=type(e).__name__,
                    error_message=format_error_message(e),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle, type(e).__name__, format_error_message(e)
                    )
                await db.commit()
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                raise
    finally:
        # Close client for non-streaming requests (streaming closes it in the handler)
        if not request.stream:
            await http_client.aclose()

    # All channels failed
    await conversation_store.finish(
        conversation_handle, "failed", int((time.time() - start_time) * 1000)
    )
    raise ChannelException(
        f"All channels failed for model '{request.model}'",
        status_code=classify_error_status(last_error),
        original_error=format_error_message(last_error),
    )


async def _handle_non_streaming_request(
    anthropic_request: AnthropicMessageRequest,
    internal_request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    request_id: str,
    conversation_id: str,
    conversation_handle: ConversationHandle,
) -> dict:
    """Handle non-streaming Anthropic message request."""
    # Make the request
    response = await adapter.make_request(internal_request)

    # Convert response to OpenAI format first
    response_data = await adapter.convert_response(response, internal_request)

    # A native Anthropic channel already returned the exact client protocol.
    anthropic_response = (
        response_data
        if response_data.get("type") == "message"
        else openai_to_anthropic_response(response_data, anthropic_request.model)
    )

    latency_ms = int((time.time() - start_time) * 1000)
    usage = accounting_service.extract_usage(response_data)
    client_ip = http_request.client.host if http_request.client else "unknown"
    await accounting_service.record_success(
        db,
        request_id=request_id,
        conversation_id=conversation_id,
        request_protocol="anthropic_messages",
        token=token,
        channel=channel,
        model=anthropic_request.model,
        provider_model=adapter.map_model_name(anthropic_request.model),
        usage=usage,
        latency_ms=latency_ms,
        client_ip=client_ip,
    )
    await conversation_store.append_response(conversation_handle, response_data)
    await conversation_store.append_usage(
        conversation_handle, response_data.get("usage") or {}
    )
    await conversation_store.finish(conversation_handle, "success", latency_ms)

    await db.commit()

    return anthropic_response


async def _handle_streaming_request(
    anthropic_request: AnthropicMessageRequest,
    internal_request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    http_client: AsyncClient,
    request_id: str,
    conversation_id: str,
    response,
    conversation_handle: ConversationHandle,
    native_anthropic_stream: bool = False,
) -> StreamingResponse:
    """Handle streaming Anthropic message request."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # A StreamingResponse outlives the endpoint call, so FastAPI would retain
    # this request-scoped session (and its checked-out connection) for the
    # whole stream.  Final accounting uses a fresh, short-lived session below.
    await db.close()

    async def stream_generator():
        """Generator for streaming responses in Anthropic SSE format."""
        try:
            # Track token usage for streaming
            input_tokens = 0
            output_tokens = 0
            has_provider_usage = False
            collected_text: list[str] = []
            collected_tool_calls: list[dict] = []
            last_finish_reason: str | None = None

            # Converted providers need a synthetic Anthropic stream envelope;
            # native channels already provide the full event sequence.
            start_event = {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": anthropic_request.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                }
            }
            if not native_anthropic_stream:
                yield f"event: message_start\ndata: {_json_dumps(start_event)}\n\n"

            # Convert and stream chunks
            stream_converter = OpenAIToAnthropicStreamConverter()
            native_message: dict | None = None
            async for openai_chunk in adapter.stream_convert_response(response, internal_request):
                if native_anthropic_stream:
                    event_type = openai_chunk.get("type") or "message"
                    yield f"event: {event_type}\ndata: {_json_dumps(openai_chunk)}\n\n"
                    if event_type == "message_start":
                        native_message = openai_chunk.get("message") or native_message
                        usage = (openai_chunk.get("message") or {}).get("usage") or {}
                        input_tokens = max(input_tokens, usage.get("input_tokens", 0))
                        output_tokens = max(output_tokens, usage.get("output_tokens", 0))
                        has_provider_usage = has_provider_usage or bool(usage)
                    elif event_type == "message_delta":
                        usage = openai_chunk.get("usage") or {}
                        input_tokens = max(input_tokens, usage.get("input_tokens", 0))
                        output_tokens = max(output_tokens, usage.get("output_tokens", 0))
                        has_provider_usage = has_provider_usage or bool(usage)
                        if openai_chunk.get("delta", {}).get("stop_reason"):
                            last_finish_reason = openai_chunk["delta"]["stop_reason"]
                    continue

                # Accumulate content for archiving.
                choices = openai_chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        collected_text.append(text)
                    for tc in delta.get("tool_calls") or []:
                        collected_tool_calls.append(tc)
                    fr = choices[0].get("finish_reason")
                    if fr:
                        last_finish_reason = fr

                # Try to extract usage info if available
                usage = openai_chunk.get("usage") or {}
                if usage:
                    has_provider_usage = True
                input_tokens = max(input_tokens, usage.get("prompt_tokens", 0))
                output_tokens = max(output_tokens, usage.get("completion_tokens", 0))

                for event in stream_converter.feed(openai_chunk):
                    event_type = event["type"]
                    yield f"event: {event_type}\ndata: {_json_dumps(event)}\n\n"

            if not native_anthropic_stream:
                for event in stream_converter.finish(
                    usage={
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                    },
                ):
                    event_type = event["type"]
                    yield f"event: {event_type}\ndata: {_json_dumps(event)}\n\n"

            total_tokens = input_tokens + output_tokens
            usage = accounting_service.streaming_usage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                has_provider_usage=has_provider_usage,
            )
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            async with async_session_maker() as stream_db:
                stream_token = await stream_db.get(type(token), token.id)
                if stream_token is None:
                    raise RuntimeError(f"Token {token.id} no longer exists")
                await accounting_service.record_success(
                    stream_db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="anthropic_messages",
                    token=stream_token,
                    channel=channel,
                    model=anthropic_request.model,
                    provider_model=adapter.map_model_name(anthropic_request.model),
                    usage=usage,
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                await stream_db.commit()
            if native_anthropic_stream:
                await conversation_store.append_response(conversation_handle, {
                    "id": (native_message or {}).get("id", msg_id),
                    "type": "message",
                    "role": "assistant",
                    "content": (native_message or {}).get("content") or [],
                    "stop_reason": last_finish_reason,
                })
            else:
                await conversation_store.append_response(conversation_handle, {
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "".join(collected_text) or None,
                            "tool_calls": collected_tool_calls or None,
                        },
                        "finish_reason": last_finish_reason or "stop",
                    }],
                    "usage": {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                    },
                })
            await conversation_store.append_usage(conversation_handle, {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
            })
            await conversation_store.finish(conversation_handle, "success", latency_ms)

        except Exception as e:
            logger.error(f"Streaming error: {format_error_message(e)}")
            if should_fallback(e):
                routing_engine.mark_unavailable(anthropic_request.model, channel)
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            async with async_session_maker() as stream_db:
                await accounting_service.record_failure(
                    stream_db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="anthropic_messages",
                    token=token,
                    channel=channel,
                    model=anthropic_request.model,
                    error_code=type(e).__name__,
                    error_message=format_error_message(e),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                await stream_db.commit()
            await conversation_store.append_error(
                conversation_handle, type(e).__name__, format_error_message(e)
            )
            await conversation_store.finish(conversation_handle, "failed", latency_ms)
            # Send error in Anthropic error format
            error_event = {
                "type": "error",
                "error": {
                    "type": "stream_error",
                    "message": format_error_message(e)
                }
            }
            yield f"event: error\ndata: {_json_dumps(error_event)}\n\n"

        finally:
            # Close the HTTP client after streaming completes
            await http_client.aclose()

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

def _json_dumps(data: dict) -> str:
    """JSON dump with special handling."""
    import json
    return json.dumps(data, ensure_ascii=False)
