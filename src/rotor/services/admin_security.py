from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.models.admin_auth import (
    AdminAuthEvent,
    AdminLoginThrottle,
)


@dataclass(frozen=True, slots=True)
class AdminRequestContext:
    client_ip: str
    user_agent: str | None


def admin_request_context(request: Request) -> AdminRequestContext:
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent")
    return AdminRequestContext(
        client_ip=client_ip[:64],
        user_agent=user_agent[:255] if user_agent else None,
    )


def normalize_admin_username(username: str) -> str:
    return username.casefold()


async def login_retry_after_seconds(
    db: AsyncSession,
    *,
    username_key: str,
    client_ip: str,
    now: datetime | None = None,
) -> int | None:
    throttle = await _get_login_throttle(
        db,
        username_key=username_key,
        client_ip=client_ip,
    )
    if throttle is None or throttle.locked_until is None:
        return None
    return _retry_after(throttle.locked_until, now or _utc_now())


async def record_login_failure(
    db: AsyncSession,
    *,
    username_key: str,
    client_ip: str,
    now: datetime | None = None,
) -> int | None:
    current_time = _as_utc(now or _utc_now())
    throttle = await _get_login_throttle(
        db,
        username_key=username_key,
        client_ip=client_ip,
        for_update=True,
    )
    if throttle is None:
        throttle = AdminLoginThrottle(
            username_key=username_key,
            client_ip=client_ip,
            failure_count=0,
            window_started_at=current_time,
        )
        try:
            async with db.begin_nested():
                db.add(throttle)
                await db.flush()
        except IntegrityError:
            throttle = await _get_login_throttle(
                db,
                username_key=username_key,
                client_ip=client_ip,
                for_update=True,
            )
            if throttle is None:
                raise

    if throttle.locked_until is not None:
        retry_after = _retry_after(throttle.locked_until, current_time)
        if retry_after is not None:
            return retry_after
        throttle.failure_count = 0
        throttle.window_started_at = current_time
        throttle.locked_until = None

    window_started_at = _as_utc(throttle.window_started_at)
    window = timedelta(
        seconds=settings.ROTOR_ADMIN_LOGIN_WINDOW_SECONDS
    )
    if current_time - window_started_at >= window:
        throttle.failure_count = 0
        throttle.window_started_at = current_time
        throttle.locked_until = None

    throttle.failure_count += 1
    if throttle.failure_count >= settings.ROTOR_ADMIN_LOGIN_MAX_FAILURES:
        throttle.locked_until = current_time + timedelta(
            seconds=settings.ROTOR_ADMIN_LOGIN_LOCK_SECONDS
        )
    await db.flush()
    if throttle.locked_until is None:
        return None
    return _retry_after(throttle.locked_until, current_time)


async def clear_login_failures(
    db: AsyncSession,
    *,
    username_key: str,
    client_ip: str,
) -> None:
    await db.execute(
        delete(AdminLoginThrottle).where(
            AdminLoginThrottle.username_key == username_key,
            AdminLoginThrottle.client_ip == client_ip,
        )
    )


def record_admin_auth_event(
    db: AsyncSession,
    *,
    username: str,
    event_type: str,
    success: bool,
    context: AdminRequestContext,
    admin_user_id: int | None = None,
    reason: str | None = None,
    session_id: int | None = None,
    created_at: datetime | None = None,
) -> None:
    db.add(AdminAuthEvent(
        admin_user_id=admin_user_id,
        username=username,
        event_type=event_type,
        success=success,
        reason=reason,
        client_ip=context.client_ip,
        user_agent=context.user_agent,
        session_id=session_id,
        created_at=created_at or _utc_now(),
    ))


async def _get_login_throttle(
    db: AsyncSession,
    *,
    username_key: str,
    client_ip: str,
    for_update: bool = False,
) -> AdminLoginThrottle | None:
    statement = select(AdminLoginThrottle).where(
        AdminLoginThrottle.username_key == username_key,
        AdminLoginThrottle.client_ip == client_ip,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.scalars(statement)).one_or_none()


def _retry_after(locked_until: datetime, now: datetime) -> int | None:
    seconds = (_as_utc(locked_until) - _as_utc(now)).total_seconds()
    if seconds <= 0:
        return None
    return max(1, math.ceil(seconds))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
