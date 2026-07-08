import time
import uuid
import logging
from typing import AsyncIterator
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import get_db
from rotor.core.deps import get_current_token, get_available_channels
from rotor.schemas.request import (
    ChatMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    Role,
)
from rotor.adapters.factory import AdapterFactory
from rotor.gateway.accounting import AccountingService
from rotor.conversations.store import ConversationHandle, ConversationStore
from rotor.gateway.routing import RoutingEngine
from rotor.models.channel import Channel
from rotor.core.exceptions import ChannelException
from httpx import AsyncClient, HTTPStatusError, TimeoutException

logger = logging.getLogger(__name__)

router = APIRouter()
accounting_service = AccountingService()
conversation_store = ConversationStore()
routing_engine = RoutingEngine()


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
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
        or f"conv_{uuid.uuid4().hex[:24]}"
    )
    client_ip = http_request.client.host if http_request.client else "unknown"

    # Create routing decision
    routing_decision = routing_engine.route(
        channels,
        model=request.model,
        token=token,
        request_protocol="openai_chat",
        required_capabilities={"stream"} if request.stream else set(),
    )
    candidates = routing_decision.candidates

    # Record start time
    start_time = time.time()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    last_error = None

    try:
        for channel in candidates:
            conversation_handle = None
            try:
                # Create adapter
                adapter = AdapterFactory.create_adapter(channel, http_client)
                conversation_handle = await conversation_store.start(
                    db,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    token=token,
                    request=request,
                    protocol="openai_chat",
                )
                await conversation_store.append_routing(conversation_handle, channel)

                # Make request
                if request.stream:
                    return await _handle_streaming_request(
                        request, adapter, channel, token, db, start_time,
                        http_request, http_client, request_id, conversation_handle
                    )
                else:
                    return await _handle_non_streaming_request(
                        request, adapter, channel, token, db, start_time, http_request,
                        request_id, conversation_handle
                    )

            except (HTTPStatusError, TimeoutException) as e:
                last_error = e
                logger.warning(f"Channel {channel.name} failed: {e}")
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
                    error_message=str(e),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(e).__name__,
                        str(e),
                    )
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                await db.commit()

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error with channel {channel.name}: {e}")
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
                    error_message=str(e),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(e).__name__,
                        str(e),
                    )
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                await db.commit()
    finally:
        # Close client for non-streaming requests (streaming closes it in the handler)
        if not request.stream:
            await http_client.aclose()

    # All channels failed
    raise ChannelException(
        f"All channels failed for model '{request.model}'",
        original_error=str(last_error)
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
    conversation_handle: ConversationHandle
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

    await accounting_service.record_success(
        db,
        request_id=request_id,
        conversation_id=conversation_handle.conversation_id,
        request_protocol="openai_chat",
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
) -> StreamingResponse:
    """Handle streaming chat completion request."""

    async def stream_generator():
        """Generator for streaming responses."""
        try:
            # Make the request
            response = await adapter.make_request(request)

            # Track token usage for streaming
            prompt_tokens = 0
            completion_tokens = 0

            async for chunk in adapter.stream_convert_response(response, request):
                yield f"data: {_json_dumps(chunk)}\n\n"

                # Try to extract usage info if available
                if "usage" in chunk:
                    prompt_tokens = max(prompt_tokens, chunk["usage"].get("prompt_tokens", 0))
                    completion_tokens = max(completion_tokens, chunk["usage"].get("completion_tokens", 0))

            # Send final [DONE]
            yield "data: [DONE]\n\n"

            # Update stats (estimated for streaming)
            total_tokens = prompt_tokens + completion_tokens
            usage_data = accounting_service.extract_usage({
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
            })
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            await accounting_service.record_success(
                db,
                request_id=request_id,
                conversation_id=conversation_handle.conversation_id,
                request_protocol="openai_chat",
                token=token,
                channel=channel,
                model=request.model,
                provider_model=adapter.map_model_name(request.model),
                usage=usage_data,
                latency_ms=latency_ms,
                client_ip=client_ip,
            )
            await conversation_store.append_usage(conversation_handle, {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            })
            await conversation_store.finish(conversation_handle, "success", latency_ms)

            await db.commit()

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            latency_ms = int((time.time() - start_time) * 1000)
            client_ip = http_request.client.host if http_request.client else "unknown"
            await accounting_service.record_failure(
                db,
                request_id=request_id,
                conversation_id=conversation_handle.conversation_id,
                request_protocol="openai_chat",
                token=token,
                channel=channel,
                model=request.model,
                error_code=type(e).__name__,
                error_message=str(e),
                latency_ms=latency_ms,
                client_ip=client_ip,
            )
            await conversation_store.append_error(conversation_handle, type(e).__name__, str(e))
            await conversation_store.finish(conversation_handle, "failed", latency_ms)
            await db.commit()

            # Send error in SSE format
            error_chunk = {
                "error": {
                    "message": str(e),
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


async def _update_stats(
    token,
    channel: Channel,
    db: AsyncSession,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency: float,
    success: bool
):
    """Update statistics for token and channel."""
    import datetime

    # Update token stats
    token.request_count += 1
    token.token_count += total_tokens
    token.last_used_at = datetime.datetime.utcnow()

    # Update channel stats
    channel.total_requests += 1
    if success:
        channel.success_requests += 1
    else:
        channel.failed_requests += 1


async def _increment_failed_stats(channel: Channel, db: AsyncSession):
    """Increment failed request stats for a channel."""
    channel.total_requests += 1
    channel.failed_requests += 1


async def _log_request(
    db: AsyncSession,
    token_id: int,
    channel_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    success: bool,
    latency: float,
    http_request: Request
):
    """Log request information."""
    try:
        # Get client IP
        client_ip = http_request.client.host if http_request.client else "unknown"

        log = RequestLog(
            token_id=token_id,
            channel_id=channel_id,
            model=model,
            request_model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            success=success,
            latency=latency,
            ip=client_ip
        )
        db.add(log)
    except Exception as e:
        logger.error(f"Failed to log request: {e}")


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
