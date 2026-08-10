"""OpenAI-compatible image generation proxy."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.application_settings import application_settings
from rotor.channels.presets import channel_option, join_api_url, provider_headers
from rotor.config import settings
from rotor.core.deps import get_available_channels, get_current_token
from rotor.core.exceptions import (
    ChannelException,
    classify_error_status,
    format_error_message,
    normalize_upstream_error,
    upstream_error_payload,
)
from rotor.database import async_session_maker, get_db
from rotor.gateway.accounting import AccountingService
from rotor.gateway.attempts import AttemptContext, attempt_recorder
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
from rotor.gateway.routing import routing_engine
from rotor.schemas.error import ErrorPhase


logger = logging.getLogger(__name__)
router = APIRouter()
accounting_service = AccountingService()


class ImageGenerationRequest(BaseModel):
    """Stable Images API routing fields while preserving provider extensions."""

    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str
    stream: bool = False
    user: str | None = None

    def provider_payload(self, provider_model: str) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        payload["model"] = provider_model
        return payload


def image_generation_url(channel) -> str:
    path = (channel.extra or {}).get("images_path") or "/images/generations"
    return join_api_url(channel.base_url, str(path))


def image_generation_headers(channel, *, stream: bool) -> dict[str, str]:
    auth_type = channel_option(
        provider=channel.type,
        protocol=channel.protocol,
        extra=channel.extra,
        name="auth_type",
    )
    headers = provider_headers(
        key=channel.key,
        auth_type=auth_type,
        extra_headers=(channel.extra or {}).get("headers"),
    )
    if stream:
        headers["accept"] = "text/event-stream"
    return headers


def _timeout() -> Timeout:
    return Timeout(
        connect=settings.CONNECT_TIMEOUT,
        read=settings.REQUEST_TIMEOUT,
        write=settings.WRITE_TIMEOUT,
        pool=settings.POOL_TIMEOUT,
    )


async def _record_success(
    db: AsyncSession,
    *,
    token,
    channel,
    request: ImageGenerationRequest,
    response_data: dict[str, Any],
    request_id: str,
    conversation_id: str,
    start_time: float,
    client_ip: str,
    attempt_context: AttemptContext | None = None,
) -> None:
    if attempt_context is not None:
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=request.model,
            provider_model=(channel.model_mapping or {}).get(
                request.model, request.model
            ),
            request_protocol="openai_images",
            outcome="success",
        )
    await accounting_service.record_success(
        db,
        request_id=request_id,
        conversation_id=conversation_id,
        request_protocol="openai_images",
        token=token,
        channel=channel,
        model=request.model,
        provider_model=(channel.model_mapping or {}).get(request.model, request.model),
        usage=accounting_service.extract_usage(response_data),
        latency_ms=int((time.time() - start_time) * 1000),
        client_ip=client_ip,
    )
    await db.commit()


async def _record_failure(
    db: AsyncSession,
    *,
    token,
    channel,
    model: str,
    error: Exception,
    request_id: str,
    conversation_id: str,
    start_time: float,
    client_ip: str,
    attempt_context: AttemptContext | None = None,
    phase: ErrorPhase = ErrorPhase.PROVIDER_REQUEST,
) -> None:
    if (
        attempt_context is not None
        and isinstance(error, (HTTPStatusError, RequestError))
    ):
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=model,
            provider_model=(channel.model_mapping or {}).get(model, model),
            request_protocol="openai_images",
            outcome="failed",
            error=normalize_upstream_error(error, phase=phase),
        )
    await accounting_service.record_failure(
        db,
        request_id=request_id,
        conversation_id=conversation_id,
        request_protocol="openai_images",
        token=token,
        channel=channel,
        model=model,
        error_code=type(error).__name__,
        error_message=format_error_message(error),
        latency_ms=int((time.time() - start_time) * 1000),
        client_ip=client_ip,
        provider_response=upstream_error_payload(error),
    )
    await db.commit()


def _parse_sse_data(block: str) -> dict[str, Any] | None:
    data = "\n".join(
        line[5:].lstrip()
        for line in block.splitlines()
        if line.startswith("data:")
    )
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _stream_image_response(
    *,
    upstream,
    http_client: AsyncClient,
    request: ImageGenerationRequest,
    channel,
    token,
    request_id: str,
    conversation_id: str,
    start_time: float,
    client_ip: str,
    attempt_context: AttemptContext | None = None,
) -> AsyncIterator[str]:
    terminal_blocks: list[str] = []
    response_data: dict[str, Any] = {}
    buffer: list[str] = []
    try:
        async for line in upstream.aiter_lines():
            buffer.append(line)
            if line:
                continue
            block = "\n".join(buffer) + "\n"
            buffer.clear()
            event = _parse_sse_data(block)
            if event and event.get("usage"):
                response_data["usage"] = event["usage"]
            if event and event.get("type") == "image_generation.completed":
                terminal_blocks.append(block)
            else:
                yield block

        if buffer:
            yield "\n".join(buffer) + "\n"

        async with async_session_maker() as stream_db:
            stream_token = await stream_db.get(type(token), token.id)
            if stream_token is None:
                raise RuntimeError(f"Token {token.id} no longer exists")
            await _record_success(
                stream_db,
                token=stream_token,
                channel=channel,
                request=request,
                response_data=response_data,
                request_id=request_id,
                conversation_id=conversation_id,
                start_time=start_time,
                client_ip=client_ip,
                attempt_context=attempt_context,
            )
        for block in terminal_blocks:
            yield block
    except Exception as exc:
        logger.exception("Image generation stream failed for request_id=%s", request_id)
        try:
            async with async_session_maker() as stream_db:
                stream_token = await stream_db.get(type(token), token.id)
                await _record_failure(
                    stream_db,
                    token=stream_token or token,
                    channel=channel,
                    model=request.model,
                    error=exc,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    start_time=start_time,
                    client_ip=client_ip,
                    attempt_context=attempt_context,
                    phase=ErrorPhase.PROVIDER_STREAM,
                )
        except Exception:
            logger.exception("Failed to record image stream failure")
        error_event = {
            "type": "error",
            "error": {"type": type(exc).__name__, "message": format_error_message(exc)},
        }
        yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
    finally:
        await upstream.aclose()
        await http_client.aclose()


@router.post("/images/generations", response_model=None)
async def generate_image(
    image_request: ImageGenerationRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    channels = await get_available_channels(image_request.model, token, db)
    conversation_id = (
        http_request.headers.get("X-Conversation-Id")
        or image_request.user
        or f"image_{uuid.uuid4().hex[:24]}"
    )
    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    client_ip = http_request.client.host if http_request.client else "unknown"
    request_origin = getattr(http_request.state, "request_origin", "client")
    agent_run_id = getattr(http_request.state, "agent_run_id", None)
    start_time = time.time()
    required = {"image_generation"}
    if image_request.stream:
        required.add("stream")
    affinity_used = application_settings.get().routing.affinity_enabled
    routing_decision = routing_engine.route(
        channels,
        model=image_request.model,
        token=token,
        request_protocol="openai_images",
        required_capabilities=required,
        affinity_key=conversation_id if affinity_used else None,
    )
    candidates = routing_decision.candidates
    accounting_service.record_routing_decision(
        db,
        request_id=request_id,
        token=token,
        model=image_request.model,
        request_protocol="openai_images",
        decision=routing_decision,
        required_capabilities=required,
        affinity_used=affinity_used,
        features={
            "stream": bool(image_request.stream),
            "size": image_request.size,
        },
    )
    await db.commit()
    if not candidates:
        raise ChannelException(
            f"No compatible image generation channel for model "
            f"'{image_request.model}'",
            status_code=503,
            original_error=f"Required capabilities: {sorted(required)}",
        )

    http_client = AsyncClient(timeout=_timeout())
    last_error: Exception | None = None
    for attempt, channel in enumerate(candidates):
        attempt_context = AttemptContext.start(
            attempt,
            request_origin=request_origin,
            agent_run_id=agent_run_id,
        )
        try:
            routing_engine.begin_attempt(image_request.model, channel)
            provider_model = (channel.model_mapping or {}).get(
                image_request.model, image_request.model
            )
            upstream_request = http_client.build_request(
                "POST",
                image_generation_url(channel),
                headers=image_generation_headers(channel, stream=image_request.stream),
                json=image_request.provider_payload(provider_model),
            )
            upstream = await http_client.send(
                upstream_request,
                stream=image_request.stream,
            )
            try:
                upstream.raise_for_status()
            except Exception:
                if image_request.stream:
                    await upstream.aread()
                await upstream.aclose()
                raise

            if image_request.stream:
                await db.close()
                response = StreamingResponse(
                    _stream_image_response(
                        upstream=upstream,
                        http_client=http_client,
                        request=image_request,
                        channel=channel,
                        token=token,
                        request_id=request_id,
                        conversation_id=conversation_id,
                        start_time=start_time,
                        client_ip=client_ip,
                        attempt_context=attempt_context,
                    ),
                    media_type="text/event-stream",
                )
                set_routing_headers(response, channel, image_request.model, attempt > 0)
                return response

            response_data = upstream.json()
            await upstream.aclose()
            await _record_success(
                db,
                token=token,
                channel=channel,
                request=image_request,
                response_data=response_data,
                request_id=request_id,
                conversation_id=conversation_id,
                start_time=start_time,
                client_ip=client_ip,
                attempt_context=attempt_context,
            )
            set_routing_headers(api_response, channel, image_request.model, attempt > 0)
            await http_client.aclose()
            return response_data
        except (HTTPStatusError, RequestError) as exc:
            last_error = exc
            await _record_failure(
                db,
                token=token,
                channel=channel,
                model=image_request.model,
                error=exc,
                request_id=request_id,
                conversation_id=conversation_id,
                start_time=start_time,
                client_ip=client_ip,
                attempt_context=attempt_context,
            )
            if should_fallback(exc):
                routing_engine.mark_unavailable(
                    image_request.model,
                    channel,
                    retry_after_seconds(exc),
                )
                continue
            await http_client.aclose()
            raise ChannelException(
                f"Image generation failed for model '{image_request.model}'",
                status_code=classify_error_status(exc),
                original_error=format_error_message(exc),
            ) from exc
        except Exception:
            await http_client.aclose()
            raise

    await http_client.aclose()
    raise ChannelException(
        f"All image generation channels failed for model '{image_request.model}'",
        status_code=classify_error_status(last_error),
        original_error=format_error_message(last_error),
    )
