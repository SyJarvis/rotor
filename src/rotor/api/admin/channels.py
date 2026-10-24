import time
from typing import List, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, func, select, update as sql_update

from rotor.database import get_db
from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.openai_integrity import ChatStreamIntegrity
from rotor.core.exceptions import format_error_message
from rotor.gateway.routing import routing_engine
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.services.zhipu_quota import (
    ZhipuQuotaError,
    fetch_zhipu_usage,
    is_zhipu_quota_channel,
)
from rotor.services.chatgpt_oauth import (
    OAuthError,
    clear_credential_values,
    consume_session,
    credential_values,
    fetch_codex_usage,
    probe_saved_models,
    resolve_oauth_credentials,
    validate_configuration,
)
from rotor.services import usage_cache
from rotor.schemas.channel import (
    ChannelCooldownResetRequest,
    ChannelCooldownResetResponse,
    ChannelCooldownState,
    ChannelCreate,
    ChannelListItem,
    ChannelResponse,
    ChannelRoutingStateResponse,
    ChannelUpdate,
    ChannelUsageResponse,
    ChannelUsageWindow,
    ProbeModelsResponse,
)
from rotor.schemas.request import (
    ChatCompletionRequest,
    ChatMessage,
    Function,
    Role,
    Tool,
)
from rotor.channels.presets import (
    channel_option,
    join_api_url,
    list_provider_presets,
    provider_defaults,
    provider_headers,
)

router = APIRouter(prefix="/channels", tags=["channels"])


class ProbeModelsRequest(BaseModel):
    """Channel config as typed into the create/edit form, before saving."""
    base_url: str = Field(..., min_length=1)
    key: str | None = Field(None, min_length=1)
    auth_type: Literal["api_key", "oauth"] = "api_key"
    type: str = Field(..., min_length=1)
    protocol: str = Field(default="openai")
    extra: dict = Field(default_factory=dict)


class SavedProbeModelsRequest(BaseModel):
    """Only connection overrides are accepted; stored credentials stay private."""
    base_url: str | None = Field(default=None, min_length=1)
    type: str | None = Field(default=None, min_length=1)
    protocol: str | None = Field(default=None, min_length=1)
    extra: dict | None = None


class ChannelTestResponse(BaseModel):
    ok: bool
    check_type: Literal["model_catalog", "oauth", "generation"] = "model_catalog"
    latency_ms: int
    status_code: int | None = None
    models: List[str] = Field(default_factory=list)
    protocol: str | None = None
    capability: str | None = None
    capability_ok: bool | None = None
    probe_model: str | None = None
    error: str | None = None


def _safe_upstream_error_text(
    exc: Exception,
    channel: Channel | None,
    limit: int,
    *,
    key: str | None = None,
) -> str:
    """Sanitized upstream error text: never echo credentials back to the client.

    format_error_message already redacts sensitive keys via
    upstream_error_payload; the key replacement is a backstop for providers
    that echo credentials inside free-form error text. The key comes from
    `channel.key`, or from `key` for probes against unsaved channel configs.
    """
    text = format_error_message(exc)
    redact_key = channel.key if channel else key
    if redact_key:
        text = text.replace(redact_key, "[redacted]")
    return text[:limit]


def _oauth_admin_id(request: Request) -> int:
    actor = getattr(request.state, "admin_actor", None)
    if actor is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Administrator login required")
    return actor.admin_user_id


def _validate_channel_auth(**kwargs) -> None:
    try:
        validate_configuration(**kwargs)
    except OAuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get("", response_model=List[ChannelListItem])
async def list_channels(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List all channels."""
    result = await db.execute(
        select(Channel)
        .offset(skip)
        .limit(limit)
        .order_by(Channel.priority.desc(), Channel.id)
    )
    channels = result.scalars().all()
    stats = await _channel_stats(db, [channel.id for channel in channels])
    return [
        _with_stats(ChannelListItem, channel, stats.get(channel.id))
        for channel in channels
    ]


@router.get("/routing-state", response_model=ChannelRoutingStateResponse)
async def get_channel_routing_state(
    db: AsyncSession = Depends(get_db),
) -> ChannelRoutingStateResponse:
    """Return the process-local cooldown state held by the routing engine.

    The state is intentionally keyed by `(model, channel_id)` because a
    retryable provider failure only suppresses one model on one channel.
    """
    result = await db.execute(select(Channel.id, Channel.name))
    channel_names = {row.id: row.name for row in result.all()}
    snapshot = routing_engine.routing_state()
    cooldowns = [
        ChannelCooldownState(
            model=model,
            channel_id=channel_id,
            channel_name=channel_names.get(channel_id),
            phase=state["phase"],
            remaining_seconds=state["remaining_seconds"],
            cooldown_seconds=state["cooldown_seconds"],
            known_channel=channel_id in channel_names,
        )
        for model, channel_id, state in snapshot
    ]
    return ChannelRoutingStateResponse(cooldowns=cooldowns)


@router.get("/presets")
async def list_channel_presets():
    """List built-in provider defaults for the management UI and API clients."""
    return list_provider_presets()


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific channel by ID."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    stats = await _channel_stats(db, [channel.id])
    return _with_stats(ChannelResponse, channel, stats.get(channel.id))


@router.post(
    "/{channel_id}/cooldown/reset",
    response_model=ChannelCooldownResetResponse,
)
async def reset_channel_cooldown(
    channel_id: int,
    payload: ChannelCooldownResetRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> ChannelCooldownResetResponse:
    """Clear process-local cooldowns for a channel (operator override).

    This only affects the in-memory circuit breaker: it never touches channel
    configuration, and it cannot mark a broken upstream as healthy. Reusing the
    channel immediately is at the operator's discretion.
    """
    existed = await db.scalar(
        select(Channel.id).where(Channel.id == channel_id)
    )
    if existed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found",
        )

    model = (payload.model or None) if payload is not None else None
    released = routing_engine.clear_cooldown(model, channel_id)
    if model is not None and model not in released:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No cooldown for model '{model}' on channel {channel_id}"
            ),
        )
    return ChannelCooldownResetResponse(channel_id=channel_id, released=released)


@router.post("", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    channel: ChannelCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new channel."""
    # Check if channel with same name exists
    existing = await db.execute(
        select(Channel).where(Channel.name == channel.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Channel with name '{channel.name}' already exists"
        )

    _validate_channel_auth(
        auth_type=channel.auth_type, provider_type=channel.type, protocol=channel.protocol,
        base_url=channel.base_url, key=channel.key, extra=channel.extra,
    )
    channel_data = channel.model_dump(exclude={"oauth_session_id"})
    if channel.auth_type == "oauth":
        if not channel.oauth_session_id:
            raise HTTPException(400, "Complete ChatGPT login before saving this channel")
        try:
            credential = await consume_session(
                db, channel.oauth_session_id, admin_user_id=_oauth_admin_id(request), channel_id=None,
            )
        except OAuthError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        channel_data.update(credential_values(credential))
    elif channel.oauth_session_id:
        raise HTTPException(400, "OAuth login is only valid for OAuth channels")
    defaults = provider_defaults(channel.type, channel.protocol)
    channel_data["extra"] = {
        "models_path": defaults["models_path"],
        "request_path": defaults["request_path"],
        "auth_type": defaults["auth_type"],
        **channel.extra,
    }
    new_channel = Channel(**channel_data)
    db.add(new_channel)
    await db.commit()
    await db.refresh(new_channel)

    return _with_stats(ChannelResponse, new_channel, None)


@router.post("/probe-models", response_model=ProbeModelsResponse)
async def probe_models_unsaved(payload: ProbeModelsRequest):
    """Probe model list from form input before the channel is saved."""
    start = time.perf_counter()
    if payload.auth_type == "oauth" or payload.type == "openai_codex":
        raise HTTPException(400, "Complete ChatGPT login before probing models")
    if not payload.key:
        raise HTTPException(400, "An API key is required")
    try:
        models = await _fetch_model_list(
            base_url=payload.base_url,
            key=payload.key,
            provider_type=payload.type,
            protocol=payload.protocol,
            models_path=channel_option(
                provider=payload.type,
                protocol=payload.protocol,
                extra=payload.extra,
                name="models_path",
            ),
            auth_type=channel_option(
                provider=payload.type,
                protocol=payload.protocol,
                extra=payload.extra,
                name="auth_type",
            ),
            extra_headers=(payload.extra or {}).get("headers"),
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider returned HTTP {exc.response.status_code}: "
                   f"{_safe_upstream_error_text(exc, None, 300, key=payload.key)}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Model probe failed: {_safe_upstream_error_text(exc, None, 300, key=payload.key)}",
        ) from exc
    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeModelsResponse(models=models, latency_ms=latency_ms, raw_count=len(models))


@router.post("/{channel_id}/probe-models", response_model=ProbeModelsResponse)
async def probe_models(
    channel_id: int,
    payload: SavedProbeModelsRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Probe a saved channel's model list without exposing its credentials."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found",
        )

    if channel.auth_type == "oauth":
        start = time.perf_counter()
        try:
            # OAuth catalogs always use the fixed Codex endpoint, ignoring form overrides.
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                models, raw_count = await probe_saved_models(db, client, channel)
        except OAuthError as exc:
            raise HTTPException(400 if exc.status_code == 401 else exc.status_code, str(exc)) from None
        return ProbeModelsResponse(
            models=models, latency_ms=int((time.perf_counter() - start) * 1000), raw_count=raw_count,
        )

    overrides = payload.model_dump(exclude_none=True) if payload else {}
    provider_type = overrides.get("type", channel.type)
    protocol = overrides.get("protocol", channel.protocol)
    extra = overrides.get("extra", channel.extra) or {}
    # Probe-only values never mutate the ORM channel or replace its stored key.
    extra = {name: extra[name] for name in ("models_path", "auth_type", "headers") if name in extra}
    start = time.perf_counter()
    try:
        models = await _fetch_model_list(
            base_url=overrides.get("base_url", channel.base_url),
            key=channel.key,
            provider_type=provider_type,
            protocol=protocol,
            models_path=channel_option(
                provider=provider_type,
                protocol=protocol,
                extra=extra,
                name="models_path",
            ),
            auth_type=channel_option(
                provider=provider_type,
                protocol=protocol,
                extra=extra,
                name="auth_type",
            ),
            extra_headers=extra.get("headers"),
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Provider returned HTTP {exc.response.status_code}: "
                f"{_safe_upstream_error_text(exc, channel, 300)}"
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Model probe failed: {_safe_upstream_error_text(exc, channel, 300)}",
        ) from exc
    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeModelsResponse(models=models, latency_ms=latency_ms, raw_count=len(models))


# Admin UI cards poll this while the page is open; cache briefly so several
# viewers (or rapid re-renders) do not multiply upstream usage calls.
_USAGE_RESET_MAX_SECONDS = 6 * 3600.0


def _window(payload: dict | None) -> ChannelUsageWindow:
    data = payload if isinstance(payload, dict) else {}
    return ChannelUsageWindow(
        used_percent=data.get("used_percent"),
        window_seconds=data.get("limit_window_seconds"),
        reset_after_seconds=data.get("reset_after_seconds"),
        reset_at=data.get("reset_at"),
    )


@router.get("/{channel_id}/oauth/usage", response_model=ChannelUsageResponse)
async def get_channel_oauth_usage(
    channel_id: int,
    refresh: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Current subscription usage for a channel with a quota provider.

    Covers ChatGPT OAuth channels (Codex plan windows) and API-key Zhipu
    channels (GLM Coding Plan rolling windows, matched by base-URL host).
    When the plan reports an exhausted window, every model on the channel is
    cooled down until the window resets so routing stops selecting it instead of
    collecting a 429 per model.
    """
    now = time.time()
    cached = usage_cache.fresh(channel_id, now=now)
    if cached and not refresh:
        return cached

    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found",
        )
    try:
        if channel.auth_type == "oauth":
            async with httpx.AsyncClient(timeout=15.0) as client:
                credential = await resolve_oauth_credentials(channel, client)
                payload = await fetch_codex_usage(client, credential)
            rate_limit = payload.get("rate_limit") or {}
            usage = ChannelUsageResponse(
                plan=payload.get("plan_type"),
                allowed=rate_limit.get("allowed"),
                limit_reached=rate_limit.get("limit_reached"),
                primary=_window(rate_limit.get("primary_window")),
                secondary=_window(rate_limit.get("secondary_window")),
                fetched_at=now,
            )
        elif is_zhipu_quota_channel(channel):
            async with httpx.AsyncClient(timeout=15.0) as client:
                usage = await fetch_zhipu_usage(client, channel)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Channel has no quota provider (OAuth or Zhipu required)",
            )
    except OAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code >= 500 else status.HTTP_502_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ZhipuQuotaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    usage_cache.store(channel_id, usage, now=now)

    if usage.limit_reached:
        primary = usage.primary or ChannelUsageWindow()
        reset_seconds = primary.reset_after_seconds
        if primary.reset_at:
            reset_seconds = min(
                max(primary.reset_at - now, 0.0), _USAGE_RESET_MAX_SECONDS
            )
        elif reset_seconds is None:
            reset_seconds = 30.0
        for model in {*(channel.models or [])}:
            routing_engine.mark_unavailable(model, channel, reset_seconds)
    return usage


@router.post("/{channel_id}/test", response_model=ChannelTestResponse)
async def test_channel(
    channel_id: int,
    capability: Literal["text", "stream", "function_call"] | None = None,
    test_model: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Check connectivity and optionally run an explicit, billable capability probe."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    start = time.perf_counter()
    try:
        if channel.auth_type == "oauth":
            async with httpx.AsyncClient(timeout=15.0) as client:
                await resolve_oauth_credentials(channel, client)
            models = list(channel.models or [])
        else:
            models = await _fetch_model_list(
                base_url=channel.base_url,
                key=channel.key,
                provider_type=channel.type,
                protocol=channel.protocol,
                models_path=channel_option(
                    provider=channel.type,
                    protocol=channel.protocol,
                    extra=channel.extra,
                    name="models_path",
                ),
                auth_type=channel_option(
                    provider=channel.type,
                    protocol=channel.protocol,
                    extra=channel.extra,
                    name="auth_type",
                ),
                extra_headers=(channel.extra or {}).get("headers"),
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        probe_model = test_model or next(iter(channel.models or []), None) or next(
            iter(models), None
        )
        if capability:
            if not probe_model:
                raise ValueError("A test model is required for capability probing")
            await _probe_generation_capability(
                channel=channel,
                model=probe_model,
                capability=capability,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=True,
            check_type="generation" if capability else "oauth" if channel.auth_type == "oauth" else "model_catalog",
            latency_ms=latency_ms,
            status_code=200,
            models=models[:20],
            protocol=channel.protocol,
            capability=capability,
            capability_ok=True if capability else None,
            probe_model=probe_model if capability else None,
        )
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=False,
            check_type="generation" if capability else "oauth" if channel.auth_type == "oauth" else "model_catalog",
            latency_ms=latency_ms,
            status_code=exc.response.status_code,
            protocol=channel.protocol,
            capability=capability,
            capability_ok=False if capability else None,
            error=_safe_upstream_error_text(exc, channel, 500),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=False,
            check_type="generation" if capability else "oauth" if channel.auth_type == "oauth" else "model_catalog",
            latency_ms=latency_ms,
            protocol=channel.protocol,
            capability=capability,
            capability_ok=False if capability else None,
            error=_safe_upstream_error_text(exc, channel, 500),
        )


async def _probe_generation_capability(
    *,
    channel: Channel,
    model: str,
    capability: Literal["text", "stream", "function_call"],
) -> None:
    """Perform an explicit generation probe through the configured adapter."""
    tools = None
    tool_choice = None
    prompt = "Reply with OK."
    if capability == "function_call":
        prompt = "Call the rotor_health_check function now."
        tools = [Tool(
            type="function",
            function=Function(
                name="rotor_health_check",
                description="Return the health status of the gateway probe.",
                parameters={"type": "object", "properties": {}},
            ),
        )]
        tool_choice = {
            "type": "function",
            "function": {"name": "rotor_health_check"},
        }
    request = ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role=Role.USER, content=prompt)],
        max_tokens=16,
        stream=capability == "stream",
        tools=tools,
        tool_choice=tool_choice,
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        adapter = AdapterFactory.create_adapter(channel, client)
        response = await adapter.make_request(request, timeout=30.0)
        if request.stream:
            try:
                integrity = ChatStreamIntegrity(
                    expected_choices=request.n,
                    upstream_status=response.status_code,
                    secret=channel.key,
                )
                saw_text = False
                async for chunk in adapter.stream_convert_response(response, request):
                    integrity.feed(chunk)
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if isinstance(content, str) and content:
                            saw_text = True
                integrity.finish()
                if not saw_text:
                    raise RuntimeError("Provider stream contained no text content")
                return
            finally:
                await response.aclose()

        converted = await adapter.convert_response(response, request)
        choices = converted.get("choices") or []
        if not choices:
            raise RuntimeError("Provider response contained no choices")
        message = choices[0].get("message") or {}
        if capability == "text" and message.get("content") is None:
            raise RuntimeError("Provider did not return text content")
        if capability == "function_call":
            if not message.get("tool_calls"):
                raise RuntimeError("Provider did not return a function call")


@router.put("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: int,
    update: ChannelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update a channel."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    if update.name is not None and update.name != channel.name:
        duplicate = await db.execute(
            select(Channel).where(
                Channel.name == update.name,
                Channel.id != channel_id,
            )
        )
        if duplicate.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Channel with name '{update.name}' already exists",
            )

    # Update fields
    update_data = update.model_dump(exclude_unset=True, exclude={"oauth_session_id"})
    auth_type = update_data.get("auth_type", channel.auth_type)
    provider_type = update_data.get("type", channel.type)
    protocol = update_data.get("protocol", channel.protocol)
    base_url = update_data.get("base_url", channel.base_url)
    key = update_data.get("key", channel.key if channel.auth_type == "api_key" else None)
    _validate_channel_auth(
        auth_type=auth_type, provider_type=provider_type, protocol=protocol,
        base_url=base_url, key=key, extra=update_data.get("extra", channel.extra) or {},
    )
    if auth_type == "oauth":
        if channel.auth_type != "oauth" and not update.oauth_session_id:
            raise HTTPException(400, "Complete ChatGPT login before saving this channel")
        if update.oauth_session_id:
            try:
                credential = await consume_session(
                    db, update.oauth_session_id, admin_user_id=_oauth_admin_id(request), channel_id=channel_id,
                )
            except OAuthError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
            update_data.update(credential_values(credential))
    elif update.oauth_session_id:
        raise HTTPException(400, "OAuth login is only valid for OAuth channels")
    elif channel.auth_type == "oauth":
        update_data.update(clear_credential_values())
    if (
        "extra" not in update_data
        and ({"type", "protocol"} & update_data.keys())
    ):
        old_defaults = provider_defaults(channel.type, channel.protocol)
        new_provider = update_data.get("type", channel.type)
        new_protocol = update_data.get("protocol", channel.protocol)
        new_defaults = provider_defaults(new_provider, new_protocol)
        migrated_extra = dict(channel.extra or {})
        for name in ("models_path", "request_path", "auth_type"):
            if not migrated_extra.get(name) or migrated_extra.get(name) == old_defaults[name]:
                migrated_extra[name] = new_defaults[name]
        update_data["extra"] = migrated_extra
    # Explicit values also clear a refresh lease claimed after the ORM read.
    # ORM dirty tracking would omit an unchanged None and leave that lease live.
    if update_data:
        await db.execute(sql_update(Channel).where(Channel.id == channel_id).values(**update_data))

    await db.commit()
    await db.refresh(channel)

    stats = await _channel_stats(db, [channel.id])
    return _with_stats(ChannelResponse, channel, stats.get(channel.id))


_MODEL_CATALOG_MAX_PAGES = 100
_MODEL_CATALOG_MAX_ITEMS = 10_000


async def _fetch_model_list(
    *,
    base_url: str,
    key: str,
    provider_type: str,
    protocol: str,
    models_path: str | None = None,
    auth_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> list[str]:
    """Fetch model IDs from a provider model-list endpoint."""
    resolved_models_path = models_path or channel_option(
        provider=provider_type,
        protocol=protocol,
        extra=None,
        name="models_path",
    )
    resolved_auth_type = auth_type or channel_option(
        provider=provider_type,
        protocol=protocol,
        extra=None,
        name="auth_type",
    )
    headers = provider_headers(
        key=key,
        auth_type=resolved_auth_type,
        extra_headers=extra_headers,
    )
    url = join_api_url(base_url, resolved_models_path, protocol=protocol)

    models: set[str] = set()
    seen_cursors: set[str] = set()
    item_count = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        for _ in range(_MODEL_CATALOG_MAX_PAGES):
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            page = _extract_model_ids(payload)
            item_count += len(page)
            if item_count > _MODEL_CATALOG_MAX_ITEMS:
                raise ValueError("Model catalog exceeds the item limit")
            models.update(page)
            more = False
            if isinstance(payload, dict):
                more = payload.get("has_more", payload.get("hasMore", False))
                if not isinstance(more, bool):
                    raise ValueError("Invalid model catalog pagination flag")
                if "has_more" in payload and "hasMore" in payload and payload["has_more"] != payload["hasMore"]:
                    raise ValueError("Conflicting model catalog pagination flags")
            if not more:
                return sorted(models)
            cursor = payload.get("last_id", payload.get("lastId"))
            if not page or not isinstance(cursor, str) or not cursor.strip() or cursor in seen_cursors:
                raise ValueError("Invalid or repeated model catalog cursor")
            seen_cursors.add(cursor)
            url = httpx.URL(url).copy_set_param("after_id", cursor)
    raise ValueError("Model catalog exceeds the page limit")


async def _channel_stats(
    db: AsyncSession,
    channel_ids: list[int],
) -> dict[int, tuple[int, int, int]]:
    if not channel_ids:
        return {}
    result = await db.execute(
        select(
            RequestLog.channel_id,
            func.count(RequestLog.id),
            func.sum(case((RequestLog.success == 1, 1), else_=0)),
            func.sum(case((RequestLog.success == 0, 1), else_=0)),
        )
        .where(RequestLog.channel_id.in_(channel_ids))
        .group_by(RequestLog.channel_id)
    )
    return {
        channel_id: (
            int(total or 0),
            int(successes or 0),
            int(failures or 0),
        )
        for channel_id, total, successes, failures in result.all()
    }


def _with_stats(schema, channel: Channel, stats):
    statistic_fields = {
        "total_requests",
        "success_requests",
        "failed_requests",
    }
    payload = {
        name: getattr(channel, name)
        for name in schema.model_fields
        if name not in statistic_fields
    }
    total, successes, failures = stats or (0, 0, 0)
    payload.update(
        total_requests=total,
        success_requests=successes,
        failed_requests=failures,
    )
    return schema.model_validate(payload)


def _extract_model_ids(payload) -> list[str]:
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("type") == "error" or payload.get("success") is False:
            raise ValueError("Provider returned a model catalog error")
        if "code" in payload and payload["code"] not in (0, 200, "0", "200"):
            raise ValueError("Provider returned a model catalog error")
        data = payload.get("data", payload.get("models"))
    else:
        data = payload
    if not isinstance(data, list):
        raise ValueError("Provider returned an invalid model catalog")
    ids = []
    for item in data:
        model_id = item.get("id") or item.get("name") if isinstance(item, dict) else item
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Provider returned an invalid model catalog entry")
        ids.append(model_id)
    return ids


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a channel."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    await db.delete(channel)
    await db.commit()

    return None


@router.post("/{channel_id}/disable", response_model=ChannelResponse)
async def disable_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Disable a channel."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    channel.enabled = False
    await db.commit()
    await db.refresh(channel)

    stats = await _channel_stats(db, [channel.id])
    return _with_stats(ChannelResponse, channel, stats.get(channel.id))


@router.post("/{channel_id}/enable", response_model=ChannelResponse)
async def enable_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Enable a channel."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    channel.enabled = True
    await db.commit()
    await db.refresh(channel)

    stats = await _channel_stats(db, [channel.id])
    return _with_stats(ChannelResponse, channel, stats.get(channel.id))
