import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.api.v1.chat import (
    _handle_non_streaming_request,
    _handle_streaming_request,
    accounting_service,
    conversation_store,
    responses_input_to_messages,
    routing_engine,
)
from rotor.adapters.factory import AdapterFactory
from rotor.core.deps import get_available_channels, get_current_token
from rotor.core.exceptions import ChannelException
from rotor.database import get_db
from rotor.schemas.request import ChatCompletionRequest
from httpx import AsyncClient, HTTPStatusError, TimeoutException


router = APIRouter()


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model: str
    input: Any
    instructions: str | None = None
    max_output_tokens: int | None = Field(None, alias="max_tokens")
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None


@router.post("/responses", response_model=None)
async def create_response(
    request: ResponsesRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    """Minimal OpenAI Responses API-compatible endpoint."""
    messages = responses_input_to_messages(request.input)
    if request.instructions:
        from rotor.schemas.request import ChatMessage, Role

        messages.insert(0, ChatMessage(role=Role.SYSTEM, content=request.instructions))

    chat_request = ChatCompletionRequest(
        model=request.model,
        messages=messages,
        max_tokens=request.max_output_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stream=request.stream,
    )

    channels = await get_available_channels(chat_request.model, token, db)
    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    conversation_id = (
        http_request.headers.get("X-Conversation-Id")
        or (request.metadata or {}).get("conversation_id")
        or f"conv_{uuid.uuid4().hex[:24]}"
    )
    client_ip = http_request.client.host if http_request.client else "unknown"
    start_time = time.time()

    routing_decision = routing_engine.route(
        channels,
        model=chat_request.model,
        token=token,
        request_protocol="openai_responses",
        required_capabilities={"stream"} if chat_request.stream else set(),
    )

    http_client = AsyncClient(timeout=120.0)
    last_error = None
    try:
        for channel in routing_decision.candidates:
            conversation_handle = None
            try:
                adapter = AdapterFactory.create_adapter(channel, http_client)
                conversation_handle = await conversation_store.start(
                    db,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    token=token,
                    request=chat_request,
                    protocol="openai_responses",
                )
                await conversation_store.append_routing(conversation_handle, channel)

                if chat_request.stream:
                    return await _handle_streaming_request(
                        chat_request,
                        adapter,
                        channel,
                        token,
                        db,
                        start_time,
                        http_request,
                        http_client,
                        request_id,
                        conversation_handle,
                    )

                response_data = await _handle_non_streaming_request(
                    chat_request,
                    adapter,
                    channel,
                    token,
                    db,
                    start_time,
                    http_request,
                    request_id,
                    conversation_handle,
                )
                return chat_response_to_response(response_data)

            except (HTTPStatusError, TimeoutException) as exc:
                last_error = exc
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_responses",
                    token=token,
                    channel=channel,
                    model=chat_request.model,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(exc).__name__,
                        str(exc),
                    )
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                await db.commit()

            except Exception as exc:
                last_error = exc
                latency_ms = int((time.time() - start_time) * 1000)
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_responses",
                    token=token,
                    channel=channel,
                    model=chat_request.model,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(exc).__name__,
                        str(exc),
                    )
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                await db.commit()
    finally:
        if not chat_request.stream:
            await http_client.aclose()

    raise ChannelException(
        f"All channels failed for model '{chat_request.model}'",
        original_error=str(last_error),
    )


def chat_response_to_response(chat_response: dict[str, Any]) -> dict[str, Any]:
    choice = (chat_response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    return {
        "id": chat_response.get("id", f"resp_{uuid.uuid4().hex[:24]}"),
        "object": "response",
        "created_at": chat_response.get("created", int(time.time())),
        "model": chat_response.get("model"),
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "usage": chat_response.get("usage"),
    }
