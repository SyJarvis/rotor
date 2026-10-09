"""Account administration, invitation registration, and personal API keys."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal, get_origin

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.api.admin.auth import _actor_response, _set_session_cookies
from rotor.application_settings import application_settings
from rotor.core.admin_auth import (AdminActor, create_admin_session, hash_admin_password,
    hash_admin_session_secret, require_admin_access, require_user_access)
from rotor.core.user_access import ensure_base_token, new_token_key
from rotor.database import get_db
from rotor.models.admin_auth import AdminSession, AdminUser, UserInvitation
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services.admin_security import (admin_request_context, record_admin_auth_event,
    login_retry_after_seconds, record_login_failure, clear_login_failures)

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_access)])
me_router = APIRouter(prefix="/me", dependencies=[Depends(require_user_access)])
auth_router = APIRouter(prefix="/auth")


def audit(db, request, actor, event, target_id=None):
    record_admin_auth_event(db, admin_user_id=actor.user_id, username=actor.username,
        event_type=event, success=True, reason=f"target:{target_id}" if target_id is not None else None,
        context=admin_request_context(request), session_id=actor.session_id)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def utc_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value


class ProfilePatch(StrictModel):
    display_name: str = Field(min_length=1, max_length=100)


class UserCreate(StrictModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=100)
    role: Literal["admin", "user"] = "user"
    quota: int | None = Field(default=0, ge=0)
    allowed_channels: list[int] | None = Field(default_factory=list, max_length=1000)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)


class UserPatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: Literal["admin", "user"] | None = None
    enabled: bool | None = None
    quota: int | None = Field(default=None, ge=0)
    allowed_channels: list[int] | None = Field(default=None, max_length=1000)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)


class PasswordReset(StrictModel):
    password: str = Field(min_length=6, max_length=128)


class InvitationCreate(StrictModel):
    expires_in_hours: int = Field(default=72, ge=1, le=720)
    quota: int | None = Field(default=0, ge=0)
    allowed_channels: list[int] | None = Field(default_factory=list, max_length=1000)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)


class Registration(StrictModel):
    invite_token: str = Field(min_length=20, max_length=200)
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=100)


class KeyCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    quota: int | None = Field(default=None, ge=0)
    allowed_channels: list[int] | None = Field(default=None, max_length=1000)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)
    expire_time: datetime | None = None

    _normalize_expiry = field_validator("expire_time")(utc_datetime)


class KeyPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    quota: int | None = Field(default=None, ge=0)
    allowed_channels: list[int] | None = Field(default=None, max_length=1000)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)
    expire_time: datetime | None = None
    enabled: bool | None = None

    _normalize_expiry = field_validator("expire_time")(utc_datetime)


def profile(user):
    return {"id": user.id, "user_id": user.id, "username": user.username,
            "display_name": user.display_name or user.username, "role": user.role,
            "enabled": user.enabled, "must_change_password": user.must_change_password,
            "quota": user.quota, "used_quota": user.used_quota,
            "allowed_channels": user.allowed_channels, "allowed_models": user.allowed_models,
            "default_token_id": user.default_token_id, "created_at": utc_datetime(user.created_at),
            "last_login_at": utc_datetime(user.last_login_at),
            "display_timezone": application_settings.get().display_timezone}


def key_info(token, user):
    fields = ("id", "name", "key", "user_id", "group", "owner_id", "is_base", "quota", "used_quota",
              "allowed_channels", "allowed_models", "enabled", "expired", "expire_time", "created_at",
              "updated_at", "last_used_at", "request_count", "token_count")
    return {**{field: utc_datetime(getattr(token, field)) for field in fields}, "is_default": token.id == user.default_token_id}


async def validate_channels(db, channel_ids):
    if channel_ids:
        ids = set((await db.scalars(select(Channel.id).where(Channel.id.in_(channel_ids)))).all())
        if ids != set(channel_ids):
            raise HTTPException(422, "Unknown channel in permissions")


async def account(db, user_id):
    user = await db.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@admin_router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    return [profile(user) for user in (await db.scalars(select(AdminUser).order_by(AdminUser.id))).all()]


@admin_router.post("/users", status_code=201)
async def create_user(payload: UserCreate, request: Request, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    await validate_channels(db, payload.allowed_channels)
    user = AdminUser(**payload.model_dump(exclude={"password"}),
                     password_hash=await asyncio.to_thread(hash_admin_password, payload.password),
                     enabled=True, must_change_password=True, used_quota=0)
    db.add(user)
    try:
        await db.flush()
        await ensure_base_token(db, user)
        audit(db, request, actor, "user_created", user.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Username already exists") from exc
    await db.refresh(user)
    return profile(user)


@admin_router.patch("/users/{user_id}")
async def update_user(user_id: int, payload: UserPatch, request: Request, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, user_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in ("role", "enabled", "display_name"):
        if field in changes and changes[field] is None:
            raise HTTPException(422, f"{field} cannot be null")
    await validate_channels(db, changes.get("allowed_channels"))
    if not changes:
        return profile(user)
    statement = update(AdminUser).where(AdminUser.id == user_id)
    if changes.get("role", user.role) != "admin" or not changes.get("enabled", user.enabled):
        # The eligibility check runs inside the UPDATE, preventing concurrent
        # administrators from both removing the last enabled administrator.
        other = AdminUser.__table__.alias("other_admin")
        statement = statement.where(
            (AdminUser.role != "admin") | (AdminUser.enabled.is_(False)) |
            exists(select(other.c.id).where(other.c.id != user_id, other.c.role == "admin", other.c.enabled.is_(True)))
        )
    result = await db.execute(statement.values(**changes).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "The last enabled administrator cannot be disabled or demoted")
    if "role" in changes or "enabled" in changes:
        await db.execute(delete(AdminSession).where(AdminSession.admin_user_id == user_id))
    audit(db, request, actor, "user_updated", user.id)
    await db.commit()
    await db.refresh(user)
    return profile(user)


@admin_router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, payload: PasswordReset, request: Request, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, user_id)
    user.password_hash = await asyncio.to_thread(hash_admin_password, payload.password)
    user.must_change_password = True
    await db.execute(delete(AdminSession).where(AdminSession.admin_user_id == user_id))
    audit(db, request, actor, "user_password_reset", user.id)
    await db.commit()
    return profile(user)


def invitation_info(invitation):
    return {field: utc_datetime(getattr(invitation, field)) for field in ("id", "created_by", "quota", "allowed_channels", "allowed_models",
        "expires_at", "used_at", "used_by", "revoked", "created_at")}


@admin_router.get("/invitations")
async def list_invitations(db: AsyncSession = Depends(get_db)):
    return [invitation_info(item) for item in (await db.scalars(select(UserInvitation).order_by(UserInvitation.id.desc()))).all()]


@admin_router.post("/invitations", status_code=201)
async def create_invitation(payload: InvitationCreate, request: Request, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    await validate_channels(db, payload.allowed_channels)
    secret = new_token_key()
    invitation = UserInvitation(secret_hash=hash_admin_session_secret(secret), created_by=actor.user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours),
        **payload.model_dump(exclude={"expires_in_hours"}))
    db.add(invitation)
    await db.flush()
    audit(db, request, actor, "invitation_created", invitation.id)
    await db.commit()
    await db.refresh(invitation)
    return {**invitation_info(invitation), "token": secret}


@admin_router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(invitation_id: int, request: Request, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    result = await db.execute(update(UserInvitation).where(UserInvitation.id == invitation_id).values(revoked=True))
    if not result.rowcount:
        raise HTTPException(404, "Invitation not found")
    audit(db, request, actor, "invitation_revoked", invitation_id)


@auth_router.post("/register", status_code=201)
async def register(payload: Registration, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    context = admin_request_context(request)
    retry_after = await login_retry_after_seconds(db, username_key="__registration__", client_ip=context.client_ip)
    if retry_after is not None:
        raise HTTPException(429, "Too many registration attempts", headers={"Retry-After": str(retry_after)})
    now = datetime.now(timezone.utc)
    valid = await db.scalar(select(UserInvitation.id).where(
        UserInvitation.secret_hash == hash_admin_session_secret(payload.invite_token),
        UserInvitation.used_at.is_(None), UserInvitation.revoked.is_(False), UserInvitation.expires_at > now))
    if valid is None:
        await record_login_failure(db, username_key="__registration__", client_ip=context.client_ip)
        await db.commit()
        raise HTTPException(400, "Invitation is invalid, expired, revoked, or already used")
    password_hash = await asyncio.to_thread(hash_admin_password, payload.password)
    now = datetime.now(timezone.utc)
    claimed = await db.execute(update(UserInvitation).where(
        UserInvitation.secret_hash == hash_admin_session_secret(payload.invite_token),
        UserInvitation.used_at.is_(None), UserInvitation.revoked.is_(False), UserInvitation.expires_at > now,
    ).values(used_at=now).returning(UserInvitation.id))
    invitation_id = claimed.scalar_one_or_none()
    if invitation_id is None:
        raise HTTPException(400, "Invitation is invalid, expired, revoked, or already used")
    invitation = await db.get(UserInvitation, invitation_id)
    user = AdminUser(username=payload.username, display_name=payload.display_name or payload.username,
        role="user", password_hash=password_hash, enabled=True, must_change_password=False,
        quota=invitation.quota, used_quota=0, allowed_channels=invitation.allowed_channels,
        allowed_models=invitation.allowed_models)
    db.add(user)
    try:
        await db.flush()
        await ensure_base_token(db, user)
        invitation.used_by = user.id
        session, secret, csrf = await create_admin_session(db, admin_user=user)
        await clear_login_failures(db, username_key="__registration__", client_ip=context.client_ip)
        record_admin_auth_event(db, admin_user_id=user.id, username=user.username, event_type="invitation_registered",
            success=True, reason=f"invitation:{invitation.id}", context=context, session_id=session.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Username already exists") from exc
    _set_session_cookies(response, session_secret=secret, csrf_secret=csrf)
    return _actor_response(AdminActor(f"admin-user-{user.id}", user.username, "session", False,
        user.id, session.id, user.role, user.display_name, user.default_token_id))


@me_router.get("")
async def get_profile(actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    return profile(await account(db, actor.user_id))


@me_router.patch("")
async def update_profile(payload: ProfilePatch, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, actor.user_id)
    user.display_name = payload.display_name
    await db.commit()
    return profile(user)


async def own_key(db, actor, token_id):
    token = await db.scalar(select(Token).where(Token.id == token_id, Token.owner_id == actor.user_id))
    if token is None:
        raise HTTPException(404, "API Key not found")
    return token


def validate_key_limits(payload, user):
    for field in ("allowed_channels", "allowed_models"):
        restriction = getattr(payload, field, None)
        ceiling = getattr(user, field)
        if ceiling is not None and restriction is not None and not set(restriction) <= set(ceiling):
            raise HTTPException(403, "API Key permissions cannot exceed account permissions")
    if payload.quota is not None and user.quota is not None and payload.quota > user.quota:
        raise HTTPException(403, "API Key quota cannot exceed account quota")


@me_router.get("/tokens")
async def my_tokens(actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, actor.user_id)
    tokens = (await db.scalars(select(Token).where(Token.owner_id == actor.user_id).order_by(Token.id))).all()
    return [key_info(token, user) for token in tokens]


@me_router.post("/tokens", status_code=201)
async def create_key(payload: KeyCreate, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, actor.user_id)
    validate_key_limits(payload, user)
    token = Token(**payload.model_dump(), key=new_token_key(), owner_id=user.id, is_base=False, enabled=True, expired=False)
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return key_info(token, user)


@me_router.patch("/tokens/{token_id}")
async def update_key(token_id: int, payload: KeyPatch, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    user = await account(db, actor.user_id)
    token = await own_key(db, actor, token_id)
    validate_key_limits(payload, user)
    changes = payload.model_dump(exclude_unset=True)
    if any(field in changes and changes[field] is None for field in ("name", "enabled")):
        raise HTTPException(422, "name and enabled cannot be null")
    for field, value in changes.items():
        setattr(token, field, value)
    await db.commit()
    await db.refresh(token)
    return key_info(token, user)


@me_router.post("/tokens/{token_id}/rotate")
async def rotate_key(token_id: int, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    token = await own_key(db, actor, token_id)
    token.key = new_token_key()
    await db.commit()
    await db.refresh(token)
    return key_info(token, await account(db, actor.user_id))


@me_router.post("/tokens/{token_id}/default")
async def set_default_key(token_id: int, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    token = await own_key(db, actor, token_id)
    user = await account(db, actor.user_id)
    result = await db.execute(update(AdminUser).where(AdminUser.id == user.id,
        exists(select(Token.id).where(Token.id == token.id, Token.owner_id == user.id)))
        .values(default_token_id=token.id).execution_options(synchronize_session=False))
    if not result.rowcount:
        raise HTTPException(409, "API Key was removed; select another default")
    await db.commit()
    await db.refresh(user)
    return key_info(token, user)


@me_router.delete("/tokens/{token_id}", status_code=204)
async def delete_key(token_id: int, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    await own_key(db, actor, token_id)
    # Atomic predicate also covers a concurrent request selecting this default.
    deleted = await db.execute(delete(Token).where(Token.id == token_id, Token.owner_id == actor.user_id,
        ~exists(select(AdminUser.id).where(AdminUser.default_token_id == token_id))))
    if not deleted.rowcount:
        raise HTTPException(409, "Select another default API Key before deleting this key")


async def bind_personal_logs(actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    db.info["log_owner_id"] = actor.user_id


from rotor.api.admin.logs import router as log_router
personal_logs_router = APIRouter(prefix="/me", dependencies=[Depends(bind_personal_logs)])
for route in log_router.routes:
    hidden_log_fields = {"capacity_snapshot", "cache_scope", "capacity_scope", "billing_scope",
                         "tariff_snapshot", "request_body", "response_body", "error_message"}
    personal_logs_router.add_api_route(
        route.path, route.endpoint, methods=list(route.methods),
        response_model=route.response_model,
        response_model_exclude={"__all__": hidden_log_fields} if get_origin(route.response_model) is list else hidden_log_fields,
    )
