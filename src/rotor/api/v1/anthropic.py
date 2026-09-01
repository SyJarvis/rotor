import asyncio
import time
import uuid
import logging
import json
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, Request, Response, status, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import async_session_maker, get_db
from rotor.core.client_session import (
    build_responses_prompt_cache_key,
    resolve_client_session,
)
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
from rotor.gateway.accounting import AccountingService, StreamingUsageAccumulator
from rotor.gateway.provider_facts import (
    extract_capacity_snapshot,
    extract_capacity_snapshot_from_error,
)
from rotor.gateway.attempts import AttemptContext, attempt_recorder
from rotor.gateway.routing import routing_engine, session_lease_success_reason
from rotor.services.session_leases import get_preferred_channel_id
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
    normalize_upstream_error,
    upstream_error_payload,
)
from rotor.schemas.error import ErrorPhase
from httpx import AsyncClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

router = APIRouter()
accounting_service = AccountingService()

_BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"
_BILLING_HEADER_REQUIRED_FIELDS = {"cc_version", "cc_entrypoint", "cch"}


def _is_claude_code_billing_header(text: object) -> bool:
    """Recognize the standalone, transient Claude Code billing system block."""
    if not isinstance(text, str) or not text.startswith(_BILLING_HEADER_PREFIX):
        return False
    if "\n" in text or "\r" in text:
        return False

    payload = text[len(_BILLING_HEADER_PREFIX):].strip()
    if not payload.endswith(";"):
        return False

    fields: dict[str, str] = {}
    for item in payload[:-1].split(";"):
        key, separator, value = item.strip().partition("=")
        if (
            not separator
            or not key
            or not key[0].isalpha()
            or not all(char.isalnum() or char in "_-" for char in key)
            or not value
            or key in fields
        ):
            return False
        fields[key] = value

    cch = fields.get("cch", "")
    return (
        _BILLING_HEADER_REQUIRED_FIELDS.issubset(fields)
        and len(cch) == 5
        and all(char in "0123456789abcdefABCDEF" for char in cch)
    )


def _anthropic_system_blocks(
    system: str | list[dict] | None,
) -> list[dict]:
    """Return system text blocks without transient client metadata."""
    if isinstance(system, str):
        first_line, separator, remainder = system.partition("\n")
        if _is_claude_code_billing_header(first_line.rstrip("\r")):
            system = remainder if separator else ""
        return [{"type": "text", "text": system}] if system else []

    blocks = system or []
    if (
        blocks
        and isinstance(blocks[0], dict)
        and set(blocks[0]).issubset({"type", "text"})
        and blocks[0].get("type") == "text"
        and _is_claude_code_billing_header(blocks[0].get("text"))
    ):
        blocks = blocks[1:]

    return [
        block
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def _anthropic_system_to_chat_text(
    system: str | list[dict] | None,
) -> Optional[str]:
    """Build cross-protocol system text without transient client metadata."""
    blocks = _anthropic_system_blocks(system)

    parts = [
        block.get("text", "")
        for block in blocks
    ]
    return "\n".join(parts) or None


def _anthropic_system_to_responses_cache_content(
    system: str | list[dict] | None,
) -> list[dict] | None:
    """Map Anthropic system cache markers to GPT-5.6 content blocks."""
    blocks = _anthropic_system_blocks(system)
    breakpoint_indexes = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block.get("cache_control"), dict)
        and block["cache_control"].get("type") == "ephemeral"
    ]
    if not breakpoint_indexes:
        return None

    # The Responses API accepts at most four new explicit breakpoints.
    enabled_breakpoints = set(breakpoint_indexes[-4:])
    content: list[dict] = []
    for index, block in enumerate(blocks):
        item = {
            "type": "input_text",
            "text": f"{block.get('text', '')}{'\n' if index < len(blocks) - 1 else ''}",
        }
        if index in enabled_breakpoints:
            item["prompt_cache_breakpoint"] = {"mode": "explicit"}
        content.append(item)
    return content


def anthropic_to_openai_request(anthropic_request: AnthropicMessageRequest) -> ChatCompletionRequest:
    """Convert Anthropic request to internal OpenAI format."""
    messages = []
    # Add system message if present (system may be a string or a list of
    # content blocks, e.g. [{"type": "text", "text": "...", "cache_control": {...}}])
    if anthropic_request.system:
        system = _anthropic_system_to_chat_text(anthropic_request.system)
        if system:
            messages.append(ChatMessage(role=Role.SYSTEM, content=system))

    # Convert Anthropic messages to the internal OpenAI-compatible format.
    for msg in anthropic_request.messages:
        if msg.role == "user":
            content = msg.content
            if isinstance(content, str):
                messages.append(ChatMessage(role=Role.USER, content=content))
                continue

            text_parts: list[str] = []
            image_parts: list[dict[str, Any]] = []

            def _flush_user_content() -> None:
                # Emit text and images as separate user messages: the text
                # stays a plain string, which Qwen-style chat templates
                # require when scanning for the user query, while images
                # become multimodal image_url parts.
                text = "".join(text_parts)
                if text:
                    messages.append(ChatMessage(role=Role.USER, content=text))
                if image_parts:
                    messages.append(ChatMessage(role=Role.USER, content=list(image_parts)))
                text_parts.clear()
                image_parts.clear()

            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    part = _anthropic_image_to_openai_part(block)
                    if part is not None:
                        image_parts.append(part)
                elif block.get("type") == "tool_result":
                    _flush_user_content()
                    messages.append(ChatMessage(
                        role=Role.TOOL,
                        content=_anthropic_block_content_to_text(block.get("content")),
                        tool_call_id=block.get("tool_use_id"),
                    ))
            _flush_user_content()
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

    # Some providers (Qwen-style chat templates) reject conversations whose
    # user turns are all tool results or empty content ("No user query found
    # in messages."). Guarantee one non-empty string user message.
    has_user_query = any(
        message.role == Role.USER and isinstance(message.content, str) and message.content.strip()
        for message in messages
    )
    if not has_user_query:
        if any(message.role == Role.TOOL for message in messages):
            fallback = "[Continue with the task based on the tool results above.]"
        elif any(
            message.role == Role.USER and isinstance(message.content, list)
            for message in messages
        ):
            fallback = "[User sent an image]"
        else:
            fallback = "[No user content]"
        # Insert right after the system prefix so tool results stay last.
        has_system = any(message.role == Role.SYSTEM for message in messages)
        messages.insert(1 if has_system else 0, ChatMessage(role=Role.USER, content=fallback))

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
        responses_cacheable_system_content=(
            _anthropic_system_to_responses_cache_content(anthropic_request.system)
        ),
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


def _anthropic_image_to_openai_part(block: dict) -> Optional[dict]:
    """Convert an Anthropic image block to an OpenAI image_url content part."""
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") == "base64":
        media_type = source.get("media_type") or "image/png"
        data = source.get("data") or ""
        if not data:
            return None
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data}"},
        }
    if source.get("type") == "url":
        url = source.get("url")
        if isinstance(url, str) and url:
            return {"type": "image_url", "image_url": {"url": url}}
    return None


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


def _openai_usage_to_anthropic(usage: dict) -> dict:
    prompt_details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
        or {}
    )
    prompt_tokens = int(
        usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    )
    cache_read_tokens = int(
        prompt_details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or 0
    )
    cache_write_tokens = int(
        prompt_details.get("cache_write_tokens")
        or usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or 0
    )
    cache_write_5m_tokens = int(
        prompt_details.get("cache_write_5m_tokens")
        or usage.get("cache_write_5m_tokens")
        or 0
    )
    cache_write_1h_tokens = int(
        prompt_details.get("cache_write_1h_tokens")
        or usage.get("cache_write_1h_tokens")
        or 0
    )
    if "uncached_tokens" in prompt_details:
        uncached_tokens = int(prompt_details.get("uncached_tokens") or 0)
    elif "prompt_cache_miss_tokens" in usage:
        uncached_tokens = int(usage.get("prompt_cache_miss_tokens") or 0)
    else:
        uncached_tokens = max(
            prompt_tokens - cache_read_tokens - cache_write_tokens,
            0,
        )
    result = {
        "input_tokens": uncached_tokens,
        "output_tokens": int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
        "cache_creation_input_tokens": cache_write_tokens,
        "cache_read_input_tokens": cache_read_tokens,
    }
    if cache_write_5m_tokens or cache_write_1h_tokens:
        result["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_write_5m_tokens,
            "ephemeral_1h_input_tokens": cache_write_1h_tokens,
        }
    return result


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
    anthropic_usage = _openai_usage_to_anthropic(
        openai_response.get("usage") or {}
    )

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
        anthropic_usage = _openai_usage_to_anthropic(usage or {})
        events.append({
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason_map.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": anthropic_usage,
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
    client_session = resolve_client_session(
        http_request.headers,
        metadata=request.metadata,
    )
    internal_request.responses_prompt_cache_key = build_responses_prompt_cache_key(
        token_id=token.id,
        model=request.model,
        session_id=client_session.session_id,
    )
    conversation_id = client_session.session_id or f"conv_{uuid.uuid4().hex[:24]}"
    client_ip = http_request.client.host if http_request.client else "unknown"
    request_origin = getattr(http_request.state, "request_origin", "client")
    agent_run_id = getattr(http_request.state, "agent_run_id", None)
    routing_settings = application_settings.get().routing
    lease_session_id = (
        client_session.session_id
        if routing_settings.affinity_enabled
        and routing_settings.session_lease_enabled
        else None
    )
    preferred_channel_id = await get_preferred_channel_id(
        db,
        token_id=token.id,
        session_id=lease_session_id,
        logical_model=request.model,
    )
    affinity_key = (
        client_session.affinity_key(token.id)
        if routing_settings.affinity_enabled
        else None
    )
    affinity_used = affinity_key is not None
    required_capabilities = {"stream"} if request.stream else set()
    routing_decision = routing_engine.route(
        channels,
        model=request.model,
        token=token,
        request_protocol="anthropic_messages",
        required_capabilities=required_capabilities,
        affinity_key=affinity_key,
        preferred_channel_id=preferred_channel_id,
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
            **client_session.routing_features(),
        },
    )
    await db.commit()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    streaming_response_returned = False
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
            attempt_context = AttemptContext.start(
                attempt,
                request_origin=request_origin,
                agent_run_id=agent_run_id,
            )
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
                        attempt_context=attempt_context,
                        lease_session_id=lease_session_id,
                        lease_migration_reason=session_lease_success_reason(
                            routing_decision, attempt
                        ),
                    )
                    set_routing_headers(result, channel, request.model, attempt > 0)
                    # Ownership of http_client transfers to the streaming
                    # generator, which closes it in its own finally block.
                    streaming_response_returned = True
                    return result
                else:
                    set_routing_headers(
                        api_response, channel, request.model, attempt > 0
                    )
                    return await _handle_non_streaming_request(
                        request, internal_request, adapter, channel, token, db, start_time,
                        http_request, request_id, conversation_id, conversation_handle,
                        attempt_context=attempt_context,
                        lease_session_id=lease_session_id,
                        lease_migration_reason=session_lease_success_reason(
                            routing_decision, attempt
                        ),
                    )

            except (HTTPStatusError, RequestError) as e:
                last_error = e
                logger.warning(f"Channel {channel.name} failed: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                error_fact = normalize_upstream_error(e)
                await attempt_recorder.record(
                    context=attempt_context,
                    request_id=request_id,
                    channel=channel,
                    requested_model=request.model,
                    provider_model=(channel.model_mapping or {}).get(
                        request.model, request.model
                    ),
                    request_protocol="anthropic_messages",
                    outcome="failed",
                    error=error_fact,
                )
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
                    capacity_snapshot=extract_capacity_snapshot_from_error(e),
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
                    capacity_snapshot=extract_capacity_snapshot_from_error(e),
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
        # Close the client unless a streaming response took ownership of it.
        # This covers non-streaming requests and streaming requests whose
        # make_request() failed on every candidate (the generator that would
        # normally close the client never started).
        if not streaming_response_returned:
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
    attempt_context: AttemptContext | None = None,
    lease_session_id: str | None = None,
    lease_migration_reason: str = "request_success",
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
    if attempt_context is not None:
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=anthropic_request.model,
            provider_model=adapter.map_model_name(anthropic_request.model),
            request_protocol="anthropic_messages",
            outcome="success",
        )
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
        capacity_snapshot=extract_capacity_snapshot(
            getattr(response, "headers", {})
        ),
        tariff_at=(
            attempt_context.started_at
            if attempt_context is not None
            else start_time
        ),
        lease_session_id=lease_session_id,
        lease_migration_reason=lease_migration_reason,
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
    attempt_context: AttemptContext | None = None,
    lease_session_id: str | None = None,
    lease_migration_reason: str = "request_success",
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
            stream_usage = StreamingUsageAccumulator()
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

            def observe_usage(provider_usage: dict) -> None:
                if not provider_usage:
                    return
                stream_usage.observe(accounting_service.extract_usage(
                    {"usage": provider_usage}
                ))

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
                        observe_usage(usage)
                    elif event_type == "message_delta":
                        usage = openai_chunk.get("usage") or {}
                        observe_usage(usage)
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
                observe_usage(usage)

                for event in stream_converter.feed(openai_chunk):
                    event_type = event["type"]
                    yield f"event: {event_type}\ndata: {_json_dumps(event)}\n\n"

            input_tokens = stream_usage.prompt_tokens
            output_tokens = stream_usage.completion_tokens
            cached_tokens = stream_usage.cached_tokens
            uncached_input_tokens = stream_usage.uncached_input_tokens
            cache_write_tokens = stream_usage.cache_write_tokens
            cache_write_5m_tokens = stream_usage.cache_write_5m_tokens
            cache_write_1h_tokens = stream_usage.cache_write_1h_tokens

            if not native_anthropic_stream:
                for event in stream_converter.finish(
                    usage={
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                        "prompt_tokens_details": {
                            "cached_tokens": cached_tokens,
                            "cache_write_tokens": cache_write_tokens,
                            "cache_write_5m_tokens": cache_write_5m_tokens,
                            "cache_write_1h_tokens": cache_write_1h_tokens,
                            "uncached_tokens": uncached_input_tokens,
                        },
                    },
                ):
                    event_type = event["type"]
                    yield f"event: {event_type}\ndata: {_json_dumps(event)}\n\n"

            total_tokens = input_tokens + output_tokens
            usage = stream_usage.to_usage_data(accounting_service)
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            async with async_session_maker() as stream_db:
                stream_token = await stream_db.get(type(token), token.id)
                if stream_token is None:
                    raise RuntimeError(f"Token {token.id} no longer exists")
                if attempt_context is not None:
                    await attempt_recorder.record(
                        context=attempt_context,
                        request_id=request_id,
                        channel=channel,
                        requested_model=anthropic_request.model,
                        provider_model=adapter.map_model_name(
                            anthropic_request.model
                        ),
                        request_protocol="anthropic_messages",
                        outcome="success",
                    )
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
                    capacity_snapshot=extract_capacity_snapshot(
                        getattr(response, "headers", {})
                    ),
                    tariff_at=(
                        attempt_context.started_at
                        if attempt_context is not None
                        else start_time
                    ),
                    lease_session_id=lease_session_id,
                    lease_migration_reason=lease_migration_reason,
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
                        "prompt_tokens_details": {
                            "cached_tokens": cached_tokens,
                            "cache_write_tokens": cache_write_tokens,
                            "cache_write_5m_tokens": cache_write_5m_tokens,
                            "cache_write_1h_tokens": cache_write_1h_tokens,
                            "uncached_tokens": uncached_input_tokens,
                        },
                    },
                })
            await conversation_store.append_usage(conversation_handle, {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "cache_write_5m_tokens": cache_write_5m_tokens,
                    "cache_write_1h_tokens": cache_write_1h_tokens,
                    "uncached_tokens": uncached_input_tokens,
                },
            })
            await conversation_store.finish(conversation_handle, "success", latency_ms)

        except asyncio.CancelledError:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                if attempt_context is not None:
                    await attempt_recorder.record(
                        context=attempt_context,
                        request_id=request_id,
                        channel=channel,
                        requested_model=anthropic_request.model,
                        provider_model=adapter.map_model_name(
                            anthropic_request.model
                        ),
                        request_protocol="anthropic_messages",
                        outcome="cancelled",
                    )
            except Exception:
                logger.exception(
                    "Recording cancelled attempt failed for request_id=%s",
                    request_id,
                )
            await conversation_store.finish(
                conversation_handle,
                "cancelled",
                latency_ms,
            )
            raise

        except Exception as e:
            logger.error(f"Streaming error: {format_error_message(e)}")
            if should_fallback(e):
                routing_engine.mark_unavailable(anthropic_request.model, channel)
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            try:
                if attempt_context is not None:
                    await attempt_recorder.record(
                        context=attempt_context,
                        request_id=request_id,
                        channel=channel,
                        requested_model=anthropic_request.model,
                        provider_model=adapter.map_model_name(
                            anthropic_request.model
                        ),
                        request_protocol="anthropic_messages",
                        outcome="failed",
                        error=normalize_upstream_error(
                            e,
                            phase=ErrorPhase.PROVIDER_STREAM,
                        ),
                    )
            except Exception:
                logger.exception(
                    "Recording failed attempt failed for request_id=%s",
                    request_id,
                )
            try:
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
                        capacity_snapshot=(
                            extract_capacity_snapshot_from_error(e)
                        ),
                    )
                    await stream_db.commit()
            except Exception:
                logger.exception(
                    "Accounting record_failure failed for request_id=%s; "
                    "sending error event to client anyway",
                    request_id,
                )
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
