from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.deps import get_current_token
from rotor.database import get_db
from rotor.models.channel import Channel
from rotor.models.token import Token


router = APIRouter()


@router.get("/models")
async def list_models(
    limit: int = Query(default=20, ge=1, le=1000),
    after_id: str | None = None,
    before_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    token: Token = Depends(get_current_token),
):
    """List local, user-facing model IDs without contacting any provider."""
    if after_id is not None and before_id is not None:
        raise HTTPException(400, "Specify only one pagination cursor")
    query = select(Channel).where(Channel.enabled.is_(True))
    if token.allowed_channels:
        query = query.where(Channel.id.in_(token.allowed_channels))
    result = await db.execute(query)
    model_ids = sorted({
        model
        for channel in result.scalars().all()
        for model in [*(channel.models or []), *(channel.model_mapping or {})]
        if isinstance(model, str) and model.strip()
    })
    cursor = after_id if after_id is not None else before_id
    if cursor is not None and cursor not in model_ids:
        # Do not distinguish a nonexistent model from one hidden by token scope.
        raise HTTPException(400, "Invalid pagination cursor")
    start, end = 0, len(model_ids)
    if before_id is not None:
        end = model_ids.index(before_id)
        start = max(0, end - limit)
        has_more = start > 0
    else:
        if after_id is not None:
            start = model_ids.index(after_id) + 1
        end = min(end, start + limit)
        has_more = end < len(model_ids)
    page = model_ids[start:end]
    return {
        "data": [
            {"id": model, "type": "model", "display_name": model,
             # Local aliases have no known upstream release date.
             "created_at": "1970-01-01T00:00:00Z"}
            for model in page
        ],
        "first_id": page[0] if page else None,
        "last_id": page[-1] if page else None,
        "has_more": has_more,
    }
