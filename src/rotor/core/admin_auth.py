import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.database import get_db
from rotor.models.admin_auth import AdminSession, AdminUser


ADMIN_SESSION_COOKIE = "rotor_admin_session"
ADMIN_CSRF_COOKIE = "rotor_admin_csrf"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdminActor:
    actor_id: str
    username: str
    auth_method: str
    must_change_password: bool
    admin_user_id: int
    session_id: int


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(bytes.fromhex(expected)),
        )
    except (TypeError, ValueError, OverflowError):
        return False
    return secrets.compare_digest(digest.hex(), expected)


async def ensure_default_admin(db: AsyncSession) -> AdminUser:
    existing = await db.scalar(select(AdminUser).limit(1))
    if existing is not None:
        return existing

    password_hash = await asyncio.to_thread(
        hash_admin_password, settings.ROTOR_DEFAULT_ADMIN_PASSWORD
    )
    admin = AdminUser(
        username=settings.ROTOR_DEFAULT_ADMIN_USERNAME,
        password_hash=password_hash,
        enabled=True,
        must_change_password=True,
    )
    db.add(admin)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await find_admin_user(
            db, settings.ROTOR_DEFAULT_ADMIN_USERNAME
        )
        if existing is None:
            raise
        return existing
    await db.refresh(admin)
    return admin


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def admin_authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid administrator credentials",
        headers={"WWW-Authenticate": "Session"},
    )


def _csrf_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid admin CSRF token",
    )


def password_change_required_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Administrator password change is required",
    )


async def find_admin_user(
    db: AsyncSession,
    username: str,
) -> AdminUser | None:
    result = await db.execute(
        select(AdminUser).where(
            AdminUser.username == username,
            AdminUser.enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def create_admin_session(
    db: AsyncSession,
    *,
    admin_user: AdminUser,
) -> tuple[AdminSession, str, str]:
    now = _utc_now()
    session_secret = secrets.token_urlsafe(32)
    csrf_secret = secrets.token_urlsafe(32)
    session = AdminSession(
        admin_user_id=admin_user.id,
        secret_hash=hash_admin_session_secret(session_secret),
        csrf_hash=hash_admin_session_secret(csrf_secret),
        last_seen_at=now,
        expires_at=now + timedelta(
            seconds=settings.ROTOR_ADMIN_SESSION_TTL_SECONDS
        ),
    )
    db.add(session)
    await db.flush()
    return session, session_secret, csrf_secret


def hash_admin_session_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def require_admin_actor(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AdminActor:
    session_secret = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not session_secret:
        raise admin_authentication_error()

    result = await db.execute(
        select(AdminSession, AdminUser)
        .join(AdminUser, AdminUser.id == AdminSession.admin_user_id)
        .where(
            AdminSession.secret_hash
            == hash_admin_session_secret(session_secret),
            AdminUser.enabled.is_(True),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise admin_authentication_error()

    session, user = row
    now = _utc_now()
    idle_deadline = _as_utc(session.last_seen_at) + timedelta(
        seconds=settings.ROTOR_ADMIN_SESSION_IDLE_SECONDS
    )
    if now >= _as_utc(session.expires_at) or now >= idle_deadline:
        raise admin_authentication_error()

    if request.method in _UNSAFE_METHODS:
        cookie_csrf = request.cookies.get(ADMIN_CSRF_COOKIE)
        header_csrf = request.headers.get("X-CSRF-Token")
        if (
            not cookie_csrf
            or not header_csrf
            or not secrets.compare_digest(cookie_csrf, header_csrf)
            or not secrets.compare_digest(
                hash_admin_session_secret(header_csrf), session.csrf_hash
            )
        ):
            raise _csrf_error()

    if now - _as_utc(session.last_seen_at) >= timedelta(seconds=60):
        session.last_seen_at = now
    return AdminActor(
        actor_id=f"admin-user-{user.id}",
        username=user.username,
        auth_method="session",
        must_change_password=user.must_change_password,
        admin_user_id=user.id,
        session_id=session.id,
    )


async def require_admin_access(
    actor: AdminActor = Depends(require_admin_actor),
) -> AdminActor:
    if actor.must_change_password:
        raise password_change_required_error()
    return actor
