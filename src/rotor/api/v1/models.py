import time
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from rotor.database import get_db
from rotor.core.deps import get_current_token, get_optional_token
from rotor.models.channel import Channel
from rotor.schemas.request import ModelInfo, ModelsResponse

router = APIRouter()


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    db: AsyncSession = Depends(get_db),
    token = Depends(get_optional_token),
):
    """
    List available models.

    Returns all models available from enabled channels.
    If a token is provided, filters models based on token's allowed channels.
    """
    # Build query
    query = select(Channel).where(Channel.enabled == True)

    # Filter by token's allowed channels if specified
    if token and token.allowed_channels:
        query = query.where(Channel.id.in_(token.allowed_channels))

    result = await db.execute(query)
    channels = result.scalars().all()

    # Collect all unique models
    models = set()
    for channel in channels:
        for model in channel.models:
            models.add(model)

    # Create model info objects
    model_list = []
    current_time = int(time.time())

    for model in sorted(models):
        # Determine the owner based on channel type
        owner = "rotor"
        for channel in channels:
            if model in channel.models:
                owner = channel.type
                break

        model_list.append(ModelInfo(
            id=model,
            created=current_time,
            owned_by=owner
        ))

    return ModelsResponse(data=model_list)
