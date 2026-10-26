"""Account-owned API keys and effective gateway permissions."""
from datetime import datetime, timezone
from dataclasses import dataclass, field
import secrets

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

from rotor.config import settings
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.token import Token


@dataclass(frozen=True, slots=True)
class AccountPrincipal:
    owner_id: int
    id: None = field(default=None, init=False)
    user_id: None = field(default=None, init=False)


async def validate_account_access(owner_id: int, db) -> AdminUser:
    owner = await db.get(AdminUser, owner_id, populate_existing=True)
    if owner is None or not owner.enabled:
        raise HTTPException(403, "Account is disabled")
    if owner.must_change_password:
        raise HTTPException(403, "Account password must be changed")
    if owner.quota is not None and owner.used_quota >= owner.quota:
        raise HTTPException(429, "Account token quota exceeded")
    return owner


async def resolve_account_principal(db, owner_id: int, model: str | None = None) -> AccountPrincipal:
    principal = AccountPrincipal(owner_id)
    await validate_account_access(owner_id, db)
    if model is not None and model not in await accessible_models(principal, db):
        raise HTTPException(403, "Model is not available to the account")
    return principal


async def reload_principal(db, principal):
    if isinstance(principal, AccountPrincipal):
        owner = await db.get(AdminUser, principal.owner_id, populate_existing=True)
        return principal if owner is not None else None
    return await db.get(type(principal), principal.id)


def principal_scope_key(principal) -> int | str:
    if isinstance(principal, AccountPrincipal):
        return f"account:{principal.owner_id}"
    if principal.id is None:
        raise ValueError("API Key identity must have a persistent ID")
    return principal.id


def new_token_key() -> str:
    return settings.API_KEY_PREFIX + secrets.token_urlsafe(32)


async def ensure_base_token(db, user: AdminUser) -> Token:
    if user.default_token_id is not None:
        return await db.get(Token, user.default_token_id)
    token = await db.scalar(select(Token).where(Token.owner_id == user.id, Token.is_base.is_(True)))
    if token is None:
        await db.execute(insert(Token).values(key=new_token_key(), name="base", owner_id=user.id, is_base=True,
            enabled=True, expired=False, used_quota=0, allowed_channels=None).on_conflict_do_nothing(
                index_elements=["owner_id"], index_where=text("is_base = 1")))
        token = await db.scalar(select(Token).where(Token.owner_id == user.id, Token.is_base.is_(True)))
    if user.default_token_id is None:
        user.default_token_id = token.id
    return token


async def token_owner(token, db) -> AdminUser | None:
    owner_id = getattr(token, "owner_id", None)
    if owner_id is None:
        return None
    owner = await db.get(AdminUser, owner_id, populate_existing=True)
    if owner is None or not owner.enabled:
        raise HTTPException(403, "Account is disabled")
    if owner.must_change_password:
        raise HTTPException(403, "Account password must be changed")
    return owner


async def validate_token_access(token, db) -> AdminUser | None:
    if isinstance(token, AccountPrincipal):
        return await validate_account_access(token.owner_id, db)
    if not token.enabled or token.expired:
        raise HTTPException(403, "API Key is disabled or expired")
    if token.expire_time:
        expires = token.expire_time
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(403, "API Key has expired")
    if token.quota is not None and (token.used_quota or 0) >= token.quota:
        raise HTTPException(429, "API Key token quota exceeded")
    owner = await token_owner(token, db)
    if owner is not None and owner.quota is not None and owner.used_quota >= owner.quota:
        raise HTTPException(429, "Account token quota exceeded")
    return owner


def _intersect(account, key):
    if account is None:
        return key
    if key is None:
        return account
    return sorted(set(account) & set(key))


async def effective_permissions(token, db):
    owner = await token_owner(token, db)
    if isinstance(token, AccountPrincipal):
        return owner.allowed_channels, owner.allowed_models
    if owner is None:
        return (token.allowed_channels or None), getattr(token, "allowed_models", None)
    return (_intersect(owner.allowed_channels, token.allowed_channels),
            _intersect(owner.allowed_models, token.allowed_models))


async def accessible_models(token, db) -> list[str]:
    await validate_token_access(token, db)
    channel_ids, model_ids = await effective_permissions(token, db)
    query = select(Channel).where(Channel.enabled.is_(True))
    if channel_ids is not None:
        query = query.where(Channel.id.in_(channel_ids))
    channels = (await db.scalars(query)).all()
    return sorted({model for channel in channels for model in [*(channel.models or []), *(channel.model_mapping or {})]
                   if model_ids is None or model in model_ids})
