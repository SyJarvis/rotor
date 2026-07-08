import secrets
from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from rotor.database import get_db
from rotor.models.token import Token
from rotor.schemas.token import TokenCreate, TokenUpdate, TokenResponse, TokenListItem
from rotor.config import settings

router = APIRouter(prefix="/tokens", tags=["tokens"])


def generate_token_key() -> str:
    """Generate a new API token key."""
    random_bytes = secrets.token_bytes(24)
    token_key = settings.API_KEY_PREFIX + secrets.token_urlsafe(32)
    return token_key


@router.get("", response_model=List[TokenListItem])
async def list_tokens(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List all tokens."""
    result = await db.execute(
        select(Token)
        .offset(skip)
        .limit(limit)
        .order_by(Token.id)
    )
    tokens = result.scalars().all()
    return tokens


@router.get("/{token_id}", response_model=TokenResponse)
async def get_token(
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific token by ID."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    return token


@router.post("", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def create_token(
    token: TokenCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new token."""
    # Check if token key already exists
    existing = await db.execute(
        select(Token).where(Token.key == token.key)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token with key '{token.key}' already exists"
        )

    new_token = Token(**token.model_dump())
    db.add(new_token)
    await db.commit()
    await db.refresh(new_token)

    return new_token


@router.post("/generate", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def generate_token(
    name: str,
    user_id: str = None,
    quota: int = None,
    group: str = "default",
    allowed_channels: List[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Generate a new token with a random key."""
    token_key = generate_token_key()

    new_token = Token(
        key=token_key,
        name=name,
        user_id=user_id,
        quota=quota,
        group=group,
        allowed_channels=allowed_channels,
        enabled=True,
        expired=False
    )
    db.add(new_token)
    await db.commit()
    await db.refresh(new_token)

    return new_token


@router.put("/{token_id}", response_model=TokenResponse)
async def update_token(
    token_id: int,
    update: TokenUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a token."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    # Update fields
    update_data = update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(token, field, value)

    await db.commit()
    await db.refresh(token)

    return token


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a token."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    await db.delete(token)
    await db.commit()

    return None


@router.post("/{token_id}/disable", response_model=TokenResponse)
async def disable_token(
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Disable a token."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    token.enabled = False
    await db.commit()
    await db.refresh(token)

    return token


@router.post("/{token_id}/enable", response_model=TokenResponse)
async def enable_token(
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Enable a token."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    token.enabled = True
    await db.commit()
    await db.refresh(token)

    return token


@router.post("/{token_id}/reset-quota", response_model=TokenResponse)
async def reset_token_quota(
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Reset a token's used quota to zero."""
    result = await db.execute(
        select(Token).where(Token.id == token_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token {token_id} not found"
        )

    token.used_quota = 0
    await db.commit()
    await db.refresh(token)

    return token
