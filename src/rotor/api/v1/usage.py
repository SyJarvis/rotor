"""Per-principal usage snapshot for gateway clients (GET /v1/usage)."""
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.deps import get_current_token
from rotor.core.user_access import effective_permissions, token_owner
from rotor.database import get_db
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services import usage_cache
from rotor.services.command_goat_quota import is_command_goat_quota_channel
from rotor.services.opencode_quota import is_opencode_quota_channel
from rotor.services.zhipu_quota import is_zhipu_quota_channel

router = APIRouter()


def _quota(used: Any, limit: Any) -> dict[str, Any] | None:
    """Rotor-side token quota; null when the principal is unlimited."""
    if limit is None:
        return None
    return {"used": used or 0, "limit": limit, "unit": "tokens"}


def _provider(channel: Channel) -> str:
    if channel.auth_type == "oauth":
        return "chatgpt"
    if is_command_goat_quota_channel(channel):
        return "command-goat"
    if is_opencode_quota_channel(channel):
        return "opencode"
    if is_zhipu_quota_channel(channel):
        return "zhipu"
    return "other"


@router.get("/usage")
async def account_usage(
    db: AsyncSession = Depends(get_db),
    token: Token = Depends(get_current_token),
):
    """
    Usage snapshot for the authenticated principal.

    Combines the principal's rotor token quota with cached upstream plan
    windows (ChatGPT, Zhipu, Command Goat and OpenCode) for every visible
    enabled channel permitted by the model scope. Windows come
    from the usage cache the admin console fills; this path never contacts an
    upstream provider. An empty list does not imply quota exhaustion. Provider
    billing and raw monetary allowances are reserved for the admin API.
    """
    owner = await token_owner(token, db)
    if owner is not None:
        quota = _quota(owner.used_quota, owner.quota)
    else:
        quota = _quota(getattr(token, "used_quota", None), getattr(token, "quota", None))

    channel_ids, model_ids = await effective_permissions(token, db)
    snapshots = usage_cache.all_fresh()
    if channel_ids is not None:
        snapshots = {cid: usage for cid, usage in snapshots.items() if cid in channel_ids}

    entries: list[dict[str, Any]] = []
    if snapshots:
        rows = (await db.scalars(
            select(Channel).where(Channel.enabled.is_(True), Channel.id.in_(list(snapshots)))
        )).all()
        for channel in sorted(rows, key=lambda row: row.id):
            # Model restrictions match exact names and mapping aliases;
            # an empty permission list grants no model access.
            offered = [*(channel.models or []), *(channel.model_mapping or {})]
            if model_ids is not None and not any(
                isinstance(model, str) and model.strip() and model in model_ids for model in offered
            ):
                continue
            provider = _provider(channel)
            if provider == "other":
                continue
            # An id alone can refer to stale credentials or a replaced channel.
            usage = usage_cache.fresh(channel.id, channel=channel)
            if usage is None:
                continue
            entry = {
                "channel_id": channel.id,
                "channel": channel.name,
                "provider": provider,
                "plan": usage.plan,
                "subscription_status": usage.subscription_status,
                "rate_limit": {
                    "primary_window": usage.primary.model_dump() if usage.primary else None,
                    "secondary_window": usage.secondary.model_dump() if usage.secondary else None,
                },
                "limit_reached": None if provider in ("opencode", "command-goat") else usage.limit_reached,
                "allowed": usage.allowed,
                "fetched_at": usage.fetched_at,
            }
            if usage.command_goat is not None:
                # Balances, costs, and raw allowances are operator-only.
                detail = usage.command_goat
                entry["command_goat"] = {
                    "window_limited": detail.window_limited,
                    "five_hour": {"exceeded": detail.five_hour.exceeded, "reset_at": detail.five_hour.reset_at}
                    if detail.five_hour else None,
                    "weekly": {"exceeded": detail.weekly.exceeded, "reset_at": detail.weekly.reset_at}
                    if detail.weekly else None,
                }
            if usage.opencode is not None:
                # Workspace billing and subscription meters are not per-client.
                windows = usage.opencode.usage
                entry["opencode"] = {"usage": windows.model_dump() if windows else None}
            entries.append(entry)

    return {"quota": quota, "channels": entries}
