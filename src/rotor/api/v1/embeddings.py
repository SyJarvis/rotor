"""OpenAI-compatible embeddings proxy."""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.application_settings import application_settings
from rotor.channels.presets import channel_option, join_api_url, provider_headers
from rotor.config import settings
from rotor.core.client_session import resolve_client_session
from rotor.core.deps import get_available_channels, get_current_token
from rotor.core.exceptions import (
    ChannelException,
    UpstreamProtocolError,
    ChannelsTemporarilyUnavailable,
    classify_error_status,
    format_error_message,
    normalize_upstream_error,
    upstream_error_payload,
)
from rotor.database import get_db
from rotor.gateway.accounting import AccountingService
from rotor.gateway.provider_facts import (
    extract_capacity_snapshot,
    extract_capacity_snapshot_from_error,
)
from rotor.gateway.attempts import AttemptContext, attempt_recorder
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
from rotor.gateway.routing import routing_engine, session_lease_success_reason
from rotor.services.session_leases import get_session_lease_preference
from rotor.schemas.error import ErrorPhase


router = APIRouter()
accounting_service = AccountingService()


class EmbeddingRequest(BaseModel):
    """Stable Embeddings API routing fields while preserving provider extensions."""

    model_config = ConfigDict(extra="allow")

    model: Annotated[StrictStr, Field(min_length=1)]
    input: StrictStr | list[StrictStr] | list[StrictInt] | list[list[StrictInt]]
    dimensions: Annotated[StrictInt, Field(gt=0)] | None = None
    encoding_format: Literal["float", "base64"] | None = None
    user: StrictStr | None = None

    @field_validator("input")
    @classmethod
    def validate_input(cls, value):
        if not value or (isinstance(value, list) and any(item == "" or item == [] for item in value)):
            raise ValueError("input must not be empty or contain empty inputs")
        tokens = value if isinstance(value, list) else []
        for item in tokens:
            if isinstance(item, int) and item < 0:
                raise ValueError("token IDs must be non-negative")
            if isinstance(item, list) and any(token < 0 for token in item):
                raise ValueError("token IDs must be non-negative")
        return value

    def provider_payload(self, provider_model: str) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        payload["model"] = provider_model
        return payload


class _Embedding(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    object: Literal["embedding"]
    index: Annotated[int, Field(ge=0)]
    embedding: Annotated[list[float], Field(min_length=1)] | Annotated[str, Field(min_length=1)]


class _EmbeddingUsage(BaseModel):
    model_config = ConfigDict(strict=True)

    prompt_tokens: Annotated[int, Field(ge=0)]
    total_tokens: Annotated[int, Field(ge=0)]


class _EmbeddingResponse(BaseModel):
    object: Literal["list"]
    data: Annotated[list[_Embedding], Field(min_length=1)]
    model: StrictStr
    usage: _EmbeddingUsage


def _response_data(upstream, request: EmbeddingRequest) -> dict[str, Any]:
    try:
        data = upstream.json()
        parsed = _EmbeddingResponse.model_validate(data)
        count = (
            len(request.input)
            if isinstance(request.input, list) and not isinstance(request.input[0], int)
            else 1
        )
        if data.get("error") is not None or sorted(item.index for item in parsed.data) != list(range(count)):
            raise ValueError("upstream returned an error or incomplete embeddings")
    except ValueError as exc:
        raise UpstreamProtocolError("Invalid upstream embeddings response") from exc
    return data


def embeddings_url(channel) -> str:
    path = (channel.extra or {}).get("embeddings_path") or "/embeddings"
    return join_api_url(channel.base_url, str(path))


def embeddings_headers(channel) -> dict[str, str]:
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
    request: EmbeddingRequest,
    response_data: dict[str, Any],
    request_id: str,
    conversation_id: str,
    start_time: float,
    client_ip: str,
    attempt_context: AttemptContext | None = None,
    attempt_latency_ms: int | None = None,
    capacity_snapshot: dict[str, str] | None = None,
    lease_session_id: str | None = None,
    lease_migration_reason: str = "request_success",
) -> None:
    if attempt_latency_ms is None and attempt_context is not None:
        attempt_latency_ms = attempt_context.elapsed_ms()
    if attempt_context is not None:
        # Join the request transaction: one commit covers the routing
        # decision, this attempt, and usage accounting below.
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=request.model,
            provider_model=(channel.model_mapping or {}).get(
                request.model, request.model
            ),
            request_protocol="openai_embeddings",
            outcome="success",
            db=db,
        )
    await accounting_service.record_success(
        db,
        request_id=request_id,
        conversation_id=conversation_id,
        request_protocol="openai_embeddings",
        token=token,
        channel=channel,
        model=request.model,
        provider_model=(channel.model_mapping or {}).get(request.model, request.model),
        usage=accounting_service.extract_usage(response_data),
        latency_ms=int((time.time() - start_time) * 1000),
        attempt_latency_ms=attempt_latency_ms,
        admission=attempt_context.admission if attempt_context is not None else None,
        client_ip=client_ip,
        capacity_snapshot=capacity_snapshot,
        tariff_at=(
            attempt_context.started_at
            if attempt_context is not None
            else start_time
        ),
        lease_session_id=lease_session_id,
        lease_migration_reason=lease_migration_reason,
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
    attempt_latency_ms: int | None = None,
    phase: ErrorPhase = ErrorPhase.PROVIDER_REQUEST,
) -> None:
    if attempt_latency_ms is None and attempt_context is not None:
        attempt_latency_ms = attempt_context.elapsed_ms()
    if (
        attempt_context is not None
        and isinstance(error, (HTTPStatusError, RequestError, UpstreamProtocolError))
    ):
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=model,
            provider_model=(channel.model_mapping or {}).get(model, model),
            request_protocol="openai_embeddings",
            outcome="failed",
            error=normalize_upstream_error(error, phase=phase),
            db=db,
        )
    await accounting_service.record_failure(
        db,
        request_id=request_id,
        conversation_id=conversation_id,
        request_protocol="openai_embeddings",
        token=token,
        channel=channel,
        model=model,
        error_code=type(error).__name__,
        error_message=format_error_message(error),
        latency_ms=int((time.time() - start_time) * 1000),
        attempt_latency_ms=attempt_latency_ms,
        admission=attempt_context.admission if attempt_context is not None else None,
        client_ip=client_ip,
        provider_response=upstream_error_payload(error),
        capacity_snapshot=extract_capacity_snapshot_from_error(error),
    )
    await db.commit()


@router.post("/embeddings", response_model=None)
async def create_embeddings(
    embedding_request: EmbeddingRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token=Depends(get_current_token),
):
    channels = await get_available_channels(embedding_request.model, token, db)
    client_session = resolve_client_session(
        http_request.headers,
        legacy_user_id=embedding_request.user,
    )
    conversation_id = client_session.session_id or f"embedding_{uuid.uuid4().hex[:24]}"
    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    client_ip = http_request.client.host if http_request.client else "unknown"
    request_origin = getattr(http_request.state, "request_origin", "client")
    agent_run_id = getattr(http_request.state, "agent_run_id", None)
    start_time = time.time()
    required = {"embeddings"}
    routing_settings = application_settings.get().routing
    lease_session_id = (
        client_session.session_id
        if routing_settings.affinity_enabled
        and routing_settings.session_lease_enabled
        else None
    )
    lease_preference = await get_session_lease_preference(
        db,
        token_id=token.id,
        session_id=lease_session_id,
        logical_model=embedding_request.model,
        request_protocol="openai_embeddings",
        channels=channels,
        active_native_channel_ids=routing_engine.available_native_channel_ids(
            channels, embedding_request.model, "openai_embeddings", required
        ),
        reassess_seconds=(
            routing_settings.session_lease_reassess_seconds
            if routing_settings.affinity_enabled and routing_engine.protocol_affinity_enabled else 0
        ),
    )
    affinity_key = (
        client_session.affinity_key(token.id)
        if routing_settings.affinity_enabled
        else None
    )
    affinity_used = affinity_key is not None
    routing_decision = routing_engine.route(
        channels,
        model=embedding_request.model,
        token=token,
        request_protocol="openai_embeddings",
        required_capabilities=required,
        affinity_key=affinity_key,
        preferred_channel_id=lease_preference.channel_id,
        lease_reassessment_due=lease_preference.reassessment_due,
    )
    candidates = routing_decision.candidates
    # Defer the routing-decision write: it shares the request transaction
    # with usage accounting below (one commit on the hot path).
    # NOTE: intentionally no flush here — the INSERT batches with the
    # final accounting commit.
    accounting_service.record_routing_decision(
        db,
        request_id=request_id,
        token=token,
        model=embedding_request.model,
        request_protocol="openai_embeddings",
        decision=routing_decision,
        required_capabilities=required,
        affinity_used=affinity_used,
        features={
            "encoding_format": embedding_request.encoding_format,
            "dimensions": embedding_request.dimensions,
            **client_session.routing_features(),
        },
    )
    if not candidates:
        if routing_decision.temporarily_unavailable:
            raise ChannelsTemporarilyUnavailable(embedding_request.model, routing_decision.retry_after_seconds)
        raise ChannelException(
            f"No compatible embeddings channel for model "
            f"'{embedding_request.model}'",
            status_code=503,
            original_error=f"Required capabilities: {sorted(required)}",
        )

    http_client = AsyncClient(timeout=_timeout())
    last_error: Exception | None = None
    try:
        for attempt, channel in enumerate(candidates):
            admission = routing_engine.admit_attempt(embedding_request.model, channel)
            if admission is None:
                continue
            attempt_context = AttemptContext.start(
                attempt,
                request_origin=request_origin,
                agent_run_id=agent_run_id,
            )
            attempt_context.admission = admission
            try:
                provider_model = (channel.model_mapping or {}).get(
                    embedding_request.model, embedding_request.model
                )
                upstream_request = http_client.build_request(
                    "POST",
                    embeddings_url(channel),
                    headers=embeddings_headers(channel),
                    json=embedding_request.provider_payload(provider_model),
                )
                upstream = await http_client.send(upstream_request)
                try:
                    upstream.raise_for_status()
                    response_data = _response_data(upstream, embedding_request)
                finally:
                    await upstream.aclose()
                admission.provider_succeeded = True
                await _record_success(
                    db,
                    token=token,
                    channel=channel,
                    request=embedding_request,
                    response_data=response_data,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    start_time=start_time,
                    client_ip=client_ip,
                    attempt_context=attempt_context,
                    capacity_snapshot=extract_capacity_snapshot(upstream.headers),
                    lease_session_id=lease_session_id,
                    lease_migration_reason=session_lease_success_reason(
                        routing_decision, attempt, channel=channel, request_protocol="openai_embeddings"
                    ),
                )
                set_routing_headers(api_response, channel, embedding_request.model, attempt > 0)
                return response_data
            except (HTTPStatusError, RequestError, UpstreamProtocolError) as exc:
                last_error = exc
                if should_fallback(exc):
                    routing_engine.mark_unavailable(embedding_request.model, channel, retry_after_seconds(exc))
                await _record_failure(
                    db,
                    token=token,
                    channel=channel,
                    model=embedding_request.model,
                    error=exc,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    start_time=start_time,
                    client_ip=client_ip,
                    attempt_context=attempt_context,
                )
                if should_fallback(exc):
                    continue
                raise ChannelException(
                    f"Embeddings failed for model '{embedding_request.model}'",
                    status_code=classify_error_status(exc),
                    original_error=format_error_message(exc),
                ) from exc
            finally:
                admission.engine.release_attempt(admission)

        if last_error is None:
            raise ChannelsTemporarilyUnavailable(embedding_request.model, routing_engine.retry_after_seconds(embedding_request.model, candidates))
        raise ChannelException(
            f"All embeddings channels failed for model '{embedding_request.model}'",
            status_code=classify_error_status(last_error),
            original_error=format_error_message(last_error),
        )
    finally:
        await http_client.aclose()
