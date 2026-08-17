"""Admin endpoints for database-backed MCP Control API credentials."""

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.database import get_db
from rotor.models.mcp_control_key import MCPControlKey
from rotor.schemas.mcp_control_key import (
    MCPControlKeyCreate,
    MCPControlKeyCreated,
    MCPControlKeyResponse,
)


router = APIRouter(prefix="/mcp-control-keys", tags=["mcp-control-keys"])


def generate_mcp_control_key() -> str:
    return f"rck_{secrets.token_urlsafe(32)}"


def hash_mcp_control_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mcp_control_key_hint(key: str) -> str:
    return f"rck_••••{key[-4:]}"


async def get_mcp_control_key(
    key_id: int,
    db: AsyncSession,
) -> MCPControlKey:
    result = await db.execute(
        select(MCPControlKey).where(MCPControlKey.id == key_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP Control Key {key_id} not found",
        )
    return key


@router.get("", response_model=list[MCPControlKeyResponse])
async def list_mcp_control_keys(
    db: AsyncSession = Depends(get_db),
) -> list[MCPControlKey]:
    result = await db.execute(
        select(MCPControlKey).order_by(MCPControlKey.id)
    )
    return list(result.scalars().all())


@router.post(
    "",
    response_model=MCPControlKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_mcp_control_key(
    payload: MCPControlKeyCreate,
    db: AsyncSession = Depends(get_db),
) -> MCPControlKeyCreated:
    key = generate_mcp_control_key()
    record = MCPControlKey(
        secret_hash=hash_mcp_control_key(key),
        key_hint=mcp_control_key_hint(key),
        name=payload.name,
        scopes=list(settings.ROTOR_CONTROL_API_SCOPES),
        enabled=True,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return MCPControlKeyCreated(
        **MCPControlKeyResponse.model_validate(record).model_dump(),
        key=key,
        control_api_url=settings.ROTOR_CONTROL_API_URL,
    )


@router.post("/{key_id}/disable", response_model=MCPControlKeyResponse)
async def disable_mcp_control_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> MCPControlKey:
    key = await get_mcp_control_key(key_id, db)
    key.enabled = False
    await db.commit()
    await db.refresh(key)
    return key


@router.post("/{key_id}/enable", response_model=MCPControlKeyResponse)
async def enable_mcp_control_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> MCPControlKey:
    key = await get_mcp_control_key(key_id, db)
    key.enabled = True
    await db.commit()
    await db.refresh(key)
    return key


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_control_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    key = await get_mcp_control_key(key_id, db)
    await db.delete(key)
    await db.commit()
