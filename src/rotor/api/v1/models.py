import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.deps import get_current_token
from rotor.core.exceptions import ModelNotFoundException
from rotor.core.user_access import effective_permissions
from rotor.database import get_db
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.schemas.request import ModelInfo, ModelsResponse

router = APIRouter()

_CAPABILITY_FIELDS = ("display_name", "max_input_tokens", "max_tokens", "capabilities")


async def _visible_channels(token: Token, db: AsyncSession) -> tuple[list[Channel], list[str] | None]:
    """Enabled channels visible to the principal plus its allowed model ids."""
    channel_ids, model_ids = await effective_permissions(token, db)
    query = select(Channel).where(Channel.enabled == True)  # noqa: E712
    if channel_ids is not None:
        query = query.where(Channel.id.in_(channel_ids))
    query = query.order_by(Channel.priority.desc(), Channel.weight.desc())
    channels = list((await db.scalars(query)).all())
    return channels, model_ids


def _offered_models(channel: Channel) -> list[str]:
    return [*(channel.models or []), *(channel.model_mapping or {})]


def _model_entry(model: str, channels: list[Channel], current_time: int) -> ModelInfo:
    entry = ModelInfo(id=model, created=current_time)
    # The highest-priority channel that carries metadata for the model wins;
    # other channels silently serve as fallbacks.
    for channel in channels:
        info = (channel.extra or {}).get("model_info")
        if not isinstance(info, dict):
            continue
        details = info.get(model)
        if not isinstance(details, dict):
            continue
        for field in _CAPABILITY_FIELDS:
            if details.get(field) is not None:
                setattr(entry, field, details[field])
        break
    return entry


def _in_scope(model: str, channels: list[Channel], model_ids: list[str] | None) -> bool:
    if model_ids is not None and model not in model_ids:
        return False
    return any(model in _offered_models(channel) for channel in channels)


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    db: AsyncSession = Depends(get_db),
    token: Token = Depends(get_current_token),
):
    """
    List models available to the authenticated principal.

    Returns every model offered by enabled channels the token or account may
    use, including model-mapping aliases. Optional capability metadata is
    merged from channel ``extra.model_info``.
    """
    channels, model_ids = await _visible_channels(token, db)
    models = sorted({
        model
        for channel in channels
        for model in _offered_models(channel)
        if isinstance(model, str) and model.strip() and (model_ids is None or model in model_ids)
    })
    current_time = int(time.time())
    return ModelsResponse(data=[_model_entry(model, channels, current_time) for model in models])


@router.get("/models/{model_id}", response_model=ModelInfo)
async def retrieve_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    token: Token = Depends(get_current_token),
):
    """
    Describe one model.

    A model the principal cannot use is reported the same way as an unknown
    model so the endpoint does not leak channel configuration.
    """
    channels, model_ids = await _visible_channels(token, db)
    if _in_scope(model_id, channels, model_ids):
        return _model_entry(model_id, channels, int(time.time()))
    raise ModelNotFoundException(model_id)
