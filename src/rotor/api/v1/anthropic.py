import time
import uuid
import logging
from typing import AsyncIterator, Optional
from fastapi import APIRouter, Depends, Request, status, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import get_db
from rotor.core.deps import get_current_token, get_available_channels
from rotor.schemas.request import (
    AnthropicMessageRequest,
    AnthropicMessageResponse,
    AnthropicUsage,
    AnthropicTextBlock,
    ErrorResponse,
    ChatCompletionRequest,
    ChatMessage,
    Role,
)
from rotor.adapters.factory import AdapterFactory
from rotor.services.loadbalancer import LoadBalancer
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.core.exceptions import ChannelException
from httpx import AsyncClient, HTTPStatusError, TimeoutException

logger = logging.getLogger(__name__)

router = APIRouter()


def anthropic_to_openai_request(anthropic_request: AnthropicMessageRequest) -> ChatCompletionRequest:
    """Convert Anthropic request to internal OpenAI format."""
    messages = []
    # Add system message if present
    if anthropic_request.system:
        messages.append(ChatMessage(role=Role.SYSTEM, content=anthropic_request.system))

    # Convert Anthropic messages to OpenAI format
    for msg in anthropic_request.messages:
        if msg.role == "user":
            # Handle content (could be string or list of blocks)
            content = msg.content
            if isinstance(content, list):
                # Extract text from content blocks
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "".join(text_parts) if text_parts else None
            messages.append(ChatMessage(role=Role.USER, content=content))
        elif msg.role == "assistant":
            content = msg.content
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "".join(text_parts) if text_parts else None
            messages.append(ChatMessage(role=Role.ASSISTANT, content=content))

    return ChatCompletionRequest(
        model=anthropic_request.model,
        messages=messages,
        max_tokens=anthropic_request.max_tokens,
        temperature=anthropic_request.temperature,
        top_p=anthropic_request.top_p,
        stream=anthropic_request.stream or False,
    )


def openai_to_anthropic_response(openai_response: dict, model: str) -> dict:
    """Convert OpenAI format response to Anthropic format."""
    import datetime

    # Extract content and tool calls
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    tool_calls = message.get("tool_calls", [])

    # Build content blocks
    content_blocks = []
    if content:
        content_blocks.append({
            "type": "text",
            "text": content
        })

    # Handle tool calls
    for tool_call in tool_calls:
        import json
        function = tool_call.get("function", {})
        content_blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id", ""),
            "name": function.get("name", ""),
            "input": json.loads(function.get("arguments", "{}"))
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
    usage = openai_response.get("usage", {})
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
        "usage": anthropic_usage
    }


def openai_stream_to_anthropic_chunk(openai_chunk: dict, model: str, msg_id: str) -> Optional[dict]:
    """Convert OpenAI streaming chunk to Anthropic format."""
    choices = openai_chunk.get("choices", [])
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")

    # Handle role delta (message_start)
    if "role" in delta and not delta.get("content"):
        return {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None
            }
        }

    # Handle content delta (content_block_delta)
    if "content" in delta:
        content = delta.get("content", "")
        if content:
            return {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": content
                },
                "content_block_type": "text"
            }

    # Handle finish reason (message_stop)
    if finish_reason:
        finish_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "stop_sequence",
        }
        return {
            "type": "message_stop",
            "message": {
                "stop_reason": finish_reason_map.get(finish_reason, "end_turn")
            }
        }

    return None


@router.post("/messages", response_model=None)
async def messages(
    request: AnthropicMessageRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token = Depends(get_current_token),
    x_api_key: str = Header(None, alias="x-api-key"),
    anthropic_version: str = Header(None, alias="anthropic-version"),
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

    # Get available channels for the model
    channels = await get_available_channels(request.model, token, db)

    # Create load balancer
    load_balancer = LoadBalancer(channels)

    # Record start time
    start_time = time.time()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    last_error = None

    try:
        for attempt in range(len(channels)):
            # Select a channel
            channel = load_balancer.select_channel()
            if not channel:
                break

            try:
                # Create adapter
                adapter = AdapterFactory.create_adapter(channel, http_client)

                # Make request
                if request.stream:
                    return await _handle_streaming_request(
                        request, internal_request, adapter, channel, token, db, start_time, http_request, http_client
                    )
                else:
                    return await _handle_non_streaming_request(
                        request, internal_request, adapter, channel, token, db, start_time, http_request
                    )

            except (HTTPStatusError, TimeoutException) as e:
                last_error = e
                logger.warning(f"Channel {channel.name} failed: {e}")
                load_balancer.mark_failed(channel)
                await _increment_failed_stats(channel, db)

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error with channel {channel.name}: {e}")
                load_balancer.mark_failed(channel)
                await _increment_failed_stats(channel, db)
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
    anthropic_request: AnthropicMessageRequest,
    internal_request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request
) -> dict:
    """Handle non-streaming Anthropic message request."""
    # Make the request
    response = await adapter.make_request(internal_request)

    # Convert response to OpenAI format first
    response_data = await adapter.convert_response(response, internal_request)

    # Then convert to Anthropic format
    anthropic_response = openai_to_anthropic_response(response_data, anthropic_request.model)

    # Calculate latency
    latency = time.time() - start_time

    # Extract usage info
    usage = anthropic_response.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = input_tokens + output_tokens

    # Update token quota
    if token.quota is not None:
        token.used_quota += total_tokens
        if token.used_quota >= token.quota:
            token.enabled = False
            await db.commit()

    # Update stats
    await _update_stats(
        token, channel, db,
        model=anthropic_request.model,
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        latency=latency,
        success=True
    )

    # Log request
    await _log_request(
        db, token.id, channel.id,
        anthropic_request.model, input_tokens, output_tokens,
        total_tokens, True, latency, http_request
    )

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
    http_client: AsyncClient
) -> StreamingResponse:
    """Handle streaming Anthropic message request."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    async def stream_generator():
        """Generator for streaming responses in Anthropic SSE format."""
        try:
            # Make the request
            response = await adapter.make_request(internal_request)

            # Track token usage for streaming
            input_tokens = 0
            output_tokens = 0

            # Send message_start event
            start_event = {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": anthropic_request.model,
                    "stop_reason": None
                }
            }
            yield f"event: message_start\ndata: {_json_dumps(start_event)}\n\n"

            # Send content_block_start event
            block_start_event = {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "text",
                    "text": ""
                }
            }
            yield f"event: content_block_start\ndata: {_json_dumps(block_start_event)}\n\n"

            # Convert and stream chunks
            async for openai_chunk in adapter.stream_convert_response(response, internal_request):
                anthropic_chunk = openai_stream_to_anthropic_chunk(openai_chunk, anthropic_request.model, msg_id)

                if anthropic_chunk:
                    chunk_type = anthropic_chunk.get("type")

                    # Handle content_block_delta
                    if chunk_type == "content_block_delta":
                        yield f"event: content_block_delta\ndata: {_json_dumps(anthropic_chunk)}\n\n"

                    # Handle message_stop
                    elif chunk_type == "message_stop":
                        yield f"event: message_stop\ndata: {_json_dumps(anthropic_chunk)}\n\n"
                        break

                # Try to extract usage info if available
                if "usage" in openai_chunk:
                    input_tokens = max(input_tokens, openai_chunk["usage"].get("prompt_tokens", 0))
                    output_tokens = max(output_tokens, openai_chunk["usage"].get("completion_tokens", 0))

            # Calculate latency
            latency = time.time() - start_time

            # Update stats (estimated for streaming)
            total_tokens = input_tokens + output_tokens
            await _update_stats(
                token, channel, db,
                model=anthropic_request.model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=total_tokens,
                latency=latency,
                success=True
            )

            await db.commit()

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            await _increment_failed_stats(channel, db)
            await db.commit()

            # Send error in Anthropic error format
            error_event = {
                "type": "error",
                "error": {
                    "type": "stream_error",
                    "message": str(e)
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
