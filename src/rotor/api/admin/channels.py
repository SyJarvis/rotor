import time
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from rotor.database import get_db
from rotor.models.channel import Channel
from rotor.schemas.channel import ChannelCreate, ChannelUpdate, ChannelResponse, ChannelListItem

router = APIRouter(prefix="/channels", tags=["channels"])


class ProbeModelsRequest(BaseModel):
    base_url: str
    key: str
    type: str = "openai"
    protocol: str = "openai"


class ProbeModelsResponse(BaseModel):
    models: List[str]
    latency_ms: int
    raw_count: int = 0


class ChannelTestResponse(BaseModel):
    ok: bool
    latency_ms: int
    status_code: int | None = None
    models: List[str] = Field(default_factory=list)
    error: str | None = None


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
    return channels


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

    return channel


@router.post("", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    channel: ChannelCreate,
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

    new_channel = Channel(**channel.model_dump())
    db.add(new_channel)
    await db.commit()
    await db.refresh(new_channel)

    return new_channel


@router.post("/probe-models", response_model=ProbeModelsResponse)
async def probe_models(
    probe: ProbeModelsRequest,
):
    """Probe an upstream provider model list using base_url and API key."""
    start = time.perf_counter()
    try:
        models = await _fetch_model_list(
            base_url=probe.base_url,
            key=probe.key,
            provider_type=probe.type,
            protocol=probe.protocol,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider returned HTTP {exc.response.status_code}: {exc.response.text[:300]}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Model probe failed: {exc}",
        ) from exc
    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeModelsResponse(models=models, latency_ms=latency_ms, raw_count=len(models))


@router.post("/{channel_id}/test", response_model=ChannelTestResponse)
async def test_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Check whether a saved channel can be reached and measure latency."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()

    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel {channel_id} not found"
        )

    start = time.perf_counter()
    try:
        models = await _fetch_model_list(
            base_url=channel.base_url,
            key=channel.key,
            provider_type=channel.type,
            protocol=channel.protocol,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=True,
            latency_ms=latency_ms,
            status_code=200,
            models=models[:20],
        )
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=False,
            latency_ms=latency_ms,
            status_code=exc.response.status_code,
            error=exc.response.text[:500],
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChannelTestResponse(
            ok=False,
            latency_ms=latency_ms,
            error=str(exc),
        )


@router.put("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: int,
    update: ChannelUpdate,
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

    # Update fields
    update_data = update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(channel, field, value)

    await db.commit()
    await db.refresh(channel)

    return channel


async def _fetch_model_list(
    *,
    base_url: str,
    key: str,
    provider_type: str,
    protocol: str,
) -> list[str]:
    """Fetch model IDs from a provider model-list endpoint."""
    headers = _model_headers(key=key, provider_type=provider_type, protocol=protocol)
    url = _model_url(base_url=base_url, provider_type=provider_type, protocol=protocol)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    return _extract_model_ids(payload)


def _model_url(*, base_url: str, provider_type: str, protocol: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/models"):
        return normalized
    return f"{normalized}/models"


def _model_headers(*, key: str, provider_type: str, protocol: str) -> dict[str, str]:
    if protocol == "anthropic" or provider_type == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _extract_model_ids(payload) -> list[str]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            ids = []
            for item in data:
                if isinstance(item, dict):
                    model_id = item.get("id") or item.get("name")
                    if model_id:
                        ids.append(str(model_id))
                elif isinstance(item, str):
                    ids.append(item)
            return sorted(set(ids))

        models = payload.get("models")
        if isinstance(models, list):
            ids = []
            for item in models:
                if isinstance(item, dict):
                    model_id = item.get("id") or item.get("name")
                    if model_id:
                        ids.append(str(model_id))
                elif item:
                    ids.append(str(item))
            return sorted(set(ids))

    if isinstance(payload, list):
        ids = []
        for item in payload:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    ids.append(str(model_id))
            elif item:
                ids.append(str(item))
        return sorted(set(ids))

    return []


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

    return channel


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

    return channel
