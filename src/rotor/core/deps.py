from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from rotor.database import get_db
from rotor.models.token import Token
from rotor.models.channel import Channel
from rotor.core.exceptions import AuthenticationException, ModelNotFoundException
from rotor.core.security import validate_api_key_format
from rotor.config import settings

security = HTTPBearer(auto_error=False)


async def get_current_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    db: AsyncSession = Depends(get_db)
) -> Token:
    """Validate a Rotor API key from Bearer auth or Anthropic's x-api-key."""
    token_key = credentials.credentials if credentials else x_api_key
    if not token_key:
        raise AuthenticationException("Missing Authorization or x-api-key header")

    if not validate_api_key_format(token_key):
        raise AuthenticationException("Invalid token format")

    # Query token from database
    result = await db.execute(
        select(Token).where(Token.key == token_key)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise AuthenticationException("Invalid token")

    if not token.enabled:
        raise AuthenticationException("Token is disabled")

    if token.expired:
        raise AuthenticationException("Token has expired")

    from datetime import datetime
    if token.expire_time and token.expire_time < datetime.utcnow():
        token.expired = True
        await db.commit()
        raise AuthenticationException("Token has expired")

    # Check quota
    if token.quota is not None and token.used_quota >= token.quota:
        from rotor.core.exceptions import QuotaExceededException
        raise QuotaExceededException("Token quota exceeded")

    return token


async def get_optional_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    db: AsyncSession = Depends(get_db)
) -> Optional[Token]:
    """Get optional token (doesn't raise if missing)."""
    token_key = credentials.credentials if credentials else x_api_key
    if not token_key:
        return None

    if not validate_api_key_format(token_key):
        return None

    result = await db.execute(
        select(Token).where(Token.key == token_key)
    )
    token = result.scalar_one_or_none()

    if not token or not token.enabled or token.expired:
        return None

    return token


async def get_available_channels(
    model: str,
    token: Optional[Token] = None,
    db: AsyncSession = Depends(get_db)
) -> list[Channel]:
    """Get available channels for a given model."""
    # Build query
    query = select(Channel).where(Channel.enabled.is_(True))

    # Filter by allowed channels if token has restrictions
    if token and token.allowed_channels:
        query = query.where(Channel.id.in_(token.allowed_channels))

    # Order by priority (descending) and weight
    query = query.order_by(Channel.priority.desc(), Channel.weight.desc())

    result = await db.execute(query)
    channels = [
        channel
        for channel in result.scalars().all()
        if model in (channel.models or []) or model in (channel.model_mapping or {})
    ]

    if not channels:
        raise ModelNotFoundException(model)

    return channels


async def get_channel_by_id(
    channel_id: int,
    db: AsyncSession = Depends(get_db)
) -> Channel:
    """Get a channel by ID."""
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    return channel


def get_client_ip(
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    x_real_ip: Optional[str] = Header(None, alias="X-Real-IP")
) -> str:
    """Get client IP from headers."""
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    if x_real_ip:
        return x_real_ip
    return "unknown"
