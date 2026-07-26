import time
import uuid
import logging
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import async_session_maker, get_db
from rotor.core.deps import get_current_token, get_available_channels
from rotor.application_settings import application_settings
from rotor.schemas.request import (
    ChatMessage,
    ChatCompletionRequest,
    Role,
)
from rotor.adapters.factory import AdapterFactory
from rotor.gateway.accounting import AccountingService
from rotor.conversations.store import ConversationHandle, ConversationStore
from rotor.gateway.routing import routing_engine
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
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
conversation_store = ConversationStore()


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token = Depends(get_current_token),
):
    """
    OpenAI-compatible chat completions endpoint.

    Supports:
    - Multiple providers with automatic failover
    - Streaming and non-streaming responses
    - Token usage tracking
    - Request logging
    """
    # Get available channels for the model
    channels = await get_available_channels(request.model, token, db)

    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    conversation_id = (
        http_request.headers.get("X-Conversation-Id")
        or request.user
        or f"conv_{uuid.uuid4().hex[:24]}"
    )
    client_ip = http_request.client.host if http_request.client else "unknown"

    # Create routing decision
    affinity_used = application_settings.get().routing.affinity_enabled
    required_capabilities = {"stream"} if request.stream else set()
    routing_decision = routing_engine.route(
        channels,
        model=request.model,
        token=token,
        request_protocol="openai_chat",
        required_capabilities=required_capabilities,
        affinity_key=conversation_id if affinity_used else None,
    )
    candidates = routing_decision.candidates
    accounting_service.record_routing_decision(
        db,
        request_id=request_id,
        token=token,
        model=request.model,
        request_protocol="openai_chat",
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

    # Record start time
    start_time = time.time()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    last_error = None

    conversation_handle = await conversation_store.start(
        db,
        conversation_id=conversation_id,
        request_id=request_id,
        token=token,
        request=request,
        protocol="openai_chat",
    )
    try:
        for attempt, channel in enumerate(candidates):
            try:
                routing_engine.begin_attempt(request.model, channel)
                # Create adapter
                adapter = AdapterFactory.create_adapter(channel, http_client)
                await conversation_store.append_routing(conversation_handle, channel)

                # Make request
                if request.stream:
                    response = await adapter.make_request(request)
                    result = await _handle_streaming_request(
                        request, adapter, channel, token, db, start_time,
                        http_request, http_client, request_id, conversation_handle,
                        response=response,
                    )
                    set_routing_headers(result, channel, request.model, attempt > 0)
                    return result
                else:
                    set_routing_headers(
                        api_response, channel, request.model, attempt > 0
                    )
                    return await _handle_non_streaming_request(
                        request, adapter, channel, token, db, start_time, http_request,
                        request_id, conversation_handle
                    )

            except (HTTPStatusError, RequestError) as e:
                last_error = e
                logger.warning(f"Channel {channel.name} failed: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_chat",
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
                        conversation_handle,
                        type(e).__name__,
                        format_error_message(e),
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
                    request_protocol="openai_chat",
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
                        conversation_handle,
                        type(e).__name__,
                        format_error_message(e),
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
    request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    request_id: str,
    conversation_handle: ConversationHandle,
    request_protocol: str = "openai_chat",
) -> dict:
    """Handle non-streaming chat completion request."""
    # Make the request
    response = await adapter.make_request(request)

    # Convert response
    response_data = await adapter.convert_response(response, request)

    latency_ms = int((time.time() - start_time) * 1000)
    usage = accounting_service.extract_usage(response_data)
    provider_model = adapter.map_model_name(request.model)
    client_ip = http_request.client.host if http_request.client else "unknown"

    deferred_background_usage = (
        request_protocol == "openai_responses"
        and response_data.get("object") == "response"
        and response_data.get("status") in {"queued", "in_progress"}
        and response_data.get("usage") is None
    )
    if not deferred_background_usage:
        await accounting_service.record_success(
            db,
            request_id=request_id,
            conversation_id=conversation_handle.conversation_id,
            request_protocol=request_protocol,
            token=token,
            channel=channel,
            model=request.model,
            provider_model=provider_model,
            usage=usage,
            latency_ms=latency_ms,
            client_ip=client_ip,
        )
    await conversation_store.append_response(conversation_handle, response_data)
    await conversation_store.append_usage(conversation_handle, response_data.get("usage") or {})
    await conversation_store.finish(conversation_handle, "success", latency_ms)

    await db.commit()

    return response_data


async def _handle_streaming_request(
    request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    http_client: AsyncClient,
    request_id: str,
    conversation_handle: ConversationHandle,
    request_protocol: str = "openai_chat",
    stream_transform=None,
    response=None,
    native_responses_stream: bool = False,
    native_response_callback=None,
) -> StreamingResponse:
    """Handle streaming chat completion request."""

    # FastAPI keeps yield-based dependencies alive until a StreamingResponse
    # finishes.  Release the request session now so a slow client/provider does
    # not occupy a pool connection for the lifetime of the stream.  Accounting
    # below uses a short-lived session once streaming has completed.
    await db.close()

    async def stream_generator():
        """Generator for streaming responses."""
        try:
            # Track token usage for streaming
            prompt_tokens = 0
            completion_tokens = 0
            has_provider_usage = False
            collected_text: list[str] = []
            collected_tool_calls: dict[int, dict] = {}
            last_finish_reason: str | None = None
            completed_native_response: dict | None = None
            deferred_native_terminal: dict | None = None
            accounting_recorded = False

            if stream_transform:
                for event in stream_transform.start():
                    yield f"data: {_json_dumps(event)}\n\n"

            async for chunk in adapter.stream_convert_response(response, request):
                native_event_type = chunk.get("type") if native_responses_stream else None
                if native_event_type in {
                    "response.completed",
                    "response.failed",
                    "response.cancelled",
                    "response.incomplete",
                }:
                    # Codex disconnects as soon as it receives the terminal
                    # event. Defer it until accounting has released its DB
                    # session so cancellation cannot interrupt SQLite cleanup.
                    deferred_native_terminal = chunk
                elif native_responses_stream:
                    yield f"data: {_json_dumps(chunk)}\n\n"
                elif stream_transform:
                    for event in stream_transform.feed(chunk):
                        yield f"data: {_json_dumps(event)}\n\n"
                else:
                    yield f"data: {_json_dumps(chunk)}\n\n"

                if native_responses_stream:
                    event_type = native_event_type
                    if event_type in {
                        "response.created",
                        "response.in_progress",
                        "response.completed",
                        "response.failed",
                        "response.cancelled",
                        "response.incomplete",
                    }:
                        native_response = chunk.get("response") or {}
                        if native_response_callback and native_response.get("id"):
                            await native_response_callback(native_response, False)
                        if event_type == "response.completed":
                            completed_native_response = native_response

                # Accumulate content/tool_calls/finish_reason for archiving.
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        collected_text.append(text)
                    for fallback_index, tc in enumerate(delta.get("tool_calls") or []):
                        tool_index = int(tc.get("index", fallback_index))
                        state = collected_tool_calls.setdefault(tool_index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tc.get("id"):
                            state["id"] = tc["id"]
                        if tc.get("type"):
                            state["type"] = tc["type"]
                        function = tc.get("function") or {}
                        if function.get("name"):
                            state["function"]["name"] = function["name"]
                        arguments = function.get("arguments")
                        if arguments:
                            if not isinstance(arguments, str):
                                arguments = _json_dumps(arguments)
                            state["function"]["arguments"] += arguments
                    fr = choices[0].get("finish_reason")
                    if fr:
                        last_finish_reason = fr

                # Try to extract usage info if available
                usage = chunk.get("usage") or {}
                if native_responses_stream and completed_native_response is not None:
                    usage = completed_native_response.get("usage") or {}
                if usage:
                    has_provider_usage = True
                prompt_tokens = max(
                    prompt_tokens,
                    usage.get("prompt_tokens", 0),
                    usage.get("input_tokens", 0),
                )
                completion_tokens = max(
                    completion_tokens,
                    usage.get("completion_tokens", 0),
                    usage.get("output_tokens", 0),
                )

            terminal_events: list[str] = []
            if native_responses_stream:
                if deferred_native_terminal is not None:
                    terminal_events.append(
                        f"data: {_json_dumps(deferred_native_terminal)}\n\n"
                    )
            elif stream_transform:
                for event in stream_transform.finish({
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }):
                    terminal_events.append(f"data: {_json_dumps(event)}\n\n")
            else:
                terminal_events.append("data: [DONE]\n\n")

            # Update stats (estimated for streaming)
            total_tokens = prompt_tokens + completion_tokens
            usage_data = accounting_service.streaming_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                has_provider_usage=has_provider_usage,
            )
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            # Accounting writes to SQLite; a lock collision here must never
            # mutate the response already sent to the client. Log and move on.
            try:
                async with async_session_maker() as stream_db:
                    stream_token = await stream_db.get(type(token), token.id)
                    if stream_token is None:
                        raise RuntimeError(f"Token {token.id} no longer exists")
                    await accounting_service.record_success(
                        stream_db,
                        request_id=request_id,
                        conversation_id=conversation_handle.conversation_id,
                        request_protocol=request_protocol,
                        token=stream_token,
                        channel=channel,
                        model=request.model,
                        provider_model=adapter.map_model_name(request.model),
                        usage=usage_data,
                        latency_ms=latency_ms,
                        client_ip=client_ip,
                    )
                    await stream_db.commit()
                    accounting_recorded = True
            except Exception:
                logger.exception(
                    "Accounting record_success failed for request_id=%s; "
                    "stream response already complete, continuing",
                    request_id,
                )
            if native_responses_stream and completed_native_response is not None:
                if native_response_callback and accounting_recorded:
                    await native_response_callback(completed_native_response, True)
                await conversation_store.append_response(
                    conversation_handle,
                    completed_native_response,
                )
                await conversation_store.append_usage(
                    conversation_handle,
                    completed_native_response.get("usage") or {},
                )
            else:
                await conversation_store.append_response(conversation_handle, {
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "".join(collected_text) or None,
                            "tool_calls": (
                                [
                                    collected_tool_calls[index]
                                    for index in sorted(collected_tool_calls)
                                ]
                                or None
                            ),
                        },
                        "finish_reason": last_finish_reason or "stop",
                    }],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                    },
                })
                await conversation_store.append_usage(conversation_handle, {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                })
            await conversation_store.finish(conversation_handle, "success", latency_ms)
            for terminal_event in terminal_events:
                yield terminal_event

        except Exception as e:
            logger.error(f"Streaming error: {format_error_message(e)}")
            if should_fallback(e):
                routing_engine.mark_unavailable(request.model, channel)
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            # Defensive try/except: accounting failure must not swallow the
            # error chunk we send to the client below.
            try:
                async with async_session_maker() as stream_db:
                    await accounting_service.record_failure(
                        stream_db,
                        request_id=request_id,
                        conversation_id=conversation_handle.conversation_id,
                        request_protocol=request_protocol,
                        token=token,
                        channel=channel,
                        model=request.model,
                        error_code=type(e).__name__,
                        error_message=format_error_message(e),
                        latency_ms=latency_ms,
                        client_ip=client_ip,
                    )
                    await stream_db.commit()
            except Exception:
                logger.exception(
                    "Accounting record_failure failed for request_id=%s; "
                    "sending error chunk to client anyway",
                    request_id,
                )
            await conversation_store.append_error(conversation_handle, type(e).__name__, format_error_message(e))
            await conversation_store.finish(conversation_handle, "failed", latency_ms)
            # Send an error matching the client-facing streaming protocol.
            error_message = format_error_message(e)
            if stream_transform and hasattr(stream_transform, "fail"):
                for event in stream_transform.fail(error_message, type(e).__name__):
                    yield f"data: {_json_dumps(event)}\n\n"
            elif request_protocol == "openai_responses":
                error_event = {
                    "type": "response.failed",
                    "sequence_number": 0,
                    "response": {
                        "id": f"resp_{uuid.uuid4().hex[:24]}",
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "failed",
                        "model": request.model,
                        "output": [],
                        "error": {
                            "code": type(e).__name__,
                            "message": error_message,
                        },
                    },
                }
                yield f"data: {_json_dumps(error_event)}\n\n"
            else:
                error_chunk = {
                    "error": {
                        "message": error_message,
                        "type": "stream_error"
                    }
                }
                yield f"data: {_json_dumps(error_chunk)}\n\n"

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


def responses_input_to_messages(input_data) -> list[ChatMessage]:
    """Convert a minimal OpenAI Responses API input into chat messages."""
    if isinstance(input_data, str):
        return [ChatMessage(role=Role.USER, content=input_data)]

    messages: list[ChatMessage] = []
    if isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, str):
                messages.append(ChatMessage(role=Role.USER, content=item))
                continue
            if not isinstance(item, dict):
                continue

            role = item.get("role", "user")
            # Responses API allows role="developer" (OpenAI reasoning models,
            # equivalent to system). Normalize so downstream ChatMessage stays
            # within the supported enum and provider chat APIs accept it.
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            text_parts: list[str] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") in ("input_text", "text", "output_text"):
                            text_parts.append(block.get("text", ""))
            messages.append(ChatMessage(role=Role(role), content="".join(text_parts)))

    return messages or [ChatMessage(role=Role.USER, content="")]
