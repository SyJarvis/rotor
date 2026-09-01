from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.channel import Channel
from rotor.models.session_lease import SessionLease, SessionLeaseEvent


MIN_IDLE_TTL_SECONDS = 60
MAX_IDLE_TTL_SECONDS = 86_400


@dataclass(frozen=True, slots=True)
class SessionLeaseMutation:
    event_types: tuple[str, ...]
    channel_id: int
    previous_channel_id: int | None = None


async def get_preferred_channel_id(
    db: AsyncSession,
    *,
    token_id: int,
    session_id: str | None,
    logical_model: str,
    now: datetime | None = None,
) -> int | None:
    """Return an active lease without mutating or renewing it."""
    if session_id is None:
        return None
    lease = await _get_lease(
        db,
        token_id=token_id,
        session_id=session_id,
        logical_model=logical_model,
    )
    if lease is None or _is_expired(lease, now or _utcnow()):
        return None
    return lease.channel_id


async def record_session_lease_success(
    db: AsyncSession,
    *,
    request_id: str,
    token_id: int,
    session_id: str,
    logical_model: str,
    channel_id: int,
    idle_ttl_seconds: int,
    migration_reason: str = "request_success",
    now: datetime | None = None,
) -> SessionLeaseMutation:
    """Assign, renew, or migrate a lease after an upstream success.

    The unique database key is the cross-worker source of truth. A nested
    transaction handles two workers concurrently creating the same lease
    without rolling back the caller's accounting transaction.
    """
    current_time = _as_utc(now or _utcnow())
    ttl = _valid_ttl(idle_ttl_seconds)
    expires_at = current_time + timedelta(seconds=ttl)
    lease = await _get_lease(
        db,
        token_id=token_id,
        session_id=session_id,
        logical_model=logical_model,
        for_update=True,
    )

    if lease is None:
        lease = SessionLease(
            token_id=token_id,
            session_id=session_id,
            logical_model=logical_model,
            channel_id=channel_id,
            last_used_at=current_time,
            expires_at=expires_at,
        )
        try:
            async with db.begin_nested():
                db.add(lease)
                await db.flush()
        except IntegrityError:
            # Another worker won the first-assignment race. Lock and apply
            # this successful request to the row that is now authoritative.
            lease = await _get_lease(
                db,
                token_id=token_id,
                session_id=session_id,
                logical_model=logical_model,
                for_update=True,
            )
            if lease is None:
                raise
        else:
            _add_event(
                db,
                lease=lease,
                request_id=request_id,
                event_type="assigned",
                previous_channel_id=None,
                channel_id=channel_id,
                reason="first_success",
                created_at=current_time,
            )
            return SessionLeaseMutation(
                event_types=("assigned",),
                channel_id=channel_id,
            )

    previous_channel_id = lease.channel_id
    if _is_expired(lease, current_time):
        _add_event(
            db,
            lease=lease,
            request_id=request_id,
            event_type="expired",
            previous_channel_id=previous_channel_id,
            channel_id=None,
            reason="idle_timeout",
            created_at=current_time,
        )
        lease.channel_id = channel_id
        lease.last_used_at = current_time
        lease.expires_at = expires_at
        _add_event(
            db,
            lease=lease,
            request_id=request_id,
            event_type="assigned",
            previous_channel_id=None,
            channel_id=channel_id,
            reason="post_expiry_success",
            created_at=current_time,
        )
        return SessionLeaseMutation(
            event_types=("expired", "assigned"),
            previous_channel_id=previous_channel_id,
            channel_id=channel_id,
        )

    lease.channel_id = channel_id
    lease.last_used_at = current_time
    lease.expires_at = expires_at
    if previous_channel_id == channel_id:
        event_type = "renewed"
        reason = "lease_hit"
    else:
        event_type = "migrated"
        reason = migration_reason
    _add_event(
        db,
        lease=lease,
        request_id=request_id,
        event_type=event_type,
        previous_channel_id=previous_channel_id,
        channel_id=channel_id,
        reason=reason,
        created_at=current_time,
    )
    return SessionLeaseMutation(
        event_types=(event_type,),
        previous_channel_id=previous_channel_id,
        channel_id=channel_id,
    )


def resolve_idle_ttl_seconds(
    channel: Channel,
    logical_model: str,
    default: int,
) -> int:
    """Resolve an optional per-model/channel lease TTL conservatively."""
    extra = channel.extra or {}
    per_model = extra.get("session_lease_idle_ttl_by_model")
    if isinstance(per_model, dict):
        configured = per_model.get(logical_model, per_model.get("*"))
        parsed = _parse_ttl(configured)
        if parsed is not None:
            return parsed
    parsed = _parse_ttl(extra.get("session_lease_idle_ttl_seconds"))
    if parsed is not None:
        return parsed
    return _valid_ttl(default)


async def _get_lease(
    db: AsyncSession,
    *,
    token_id: int,
    session_id: str,
    logical_model: str,
    for_update: bool = False,
) -> SessionLease | None:
    statement = select(SessionLease).where(
        SessionLease.token_id == token_id,
        SessionLease.session_id == session_id,
        SessionLease.logical_model == logical_model,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.scalars(statement)).one_or_none()


def _add_event(
    db: AsyncSession,
    *,
    lease: SessionLease,
    request_id: str,
    event_type: str,
    previous_channel_id: int | None,
    channel_id: int | None,
    reason: str,
    created_at: datetime,
) -> None:
    db.add(SessionLeaseEvent(
        lease_id=lease.id,
        request_id=request_id,
        token_id=lease.token_id,
        session_id=lease.session_id,
        logical_model=lease.logical_model,
        event_type=event_type,
        previous_channel_id=previous_channel_id,
        channel_id=channel_id,
        reason=reason,
        created_at=created_at,
    ))


def _is_expired(lease: SessionLease, now: datetime) -> bool:
    return _as_utc(lease.expires_at) <= _as_utc(now)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ttl(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if not MIN_IDLE_TTL_SECONDS <= parsed <= MAX_IDLE_TTL_SECONDS:
        return None
    return parsed


def _valid_ttl(value: int) -> int:
    parsed = _parse_ttl(value)
    if parsed is None:
        raise ValueError(
            f"idle_ttl_seconds must be between {MIN_IDLE_TTL_SECONDS} "
            f"and {MAX_IDLE_TTL_SECONDS}"
        )
    return parsed
