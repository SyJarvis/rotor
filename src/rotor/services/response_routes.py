from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.response_route import ResponseRoute


async def get_response_route(
    db: AsyncSession,
    response_id: str,
    token_id: int | None = None,
    *,
    owner_id: int | None = None,
) -> ResponseRoute | None:
    if (token_id is None) == (owner_id is None):
        raise ValueError("Response ownership requires exactly one account or API Key")
    result = await db.execute(
        select(ResponseRoute).where(
            ResponseRoute.response_id == response_id,
            ResponseRoute.token_id == token_id,
            ResponseRoute.owner_id == owner_id,
        )
    )
    return result.scalar_one_or_none()


async def save_response_route(
    db: AsyncSession,
    *,
    response_id: str,
    token_id: int | None = None,
    owner_id: int | None = None,
    channel_id: int,
    conversation_id: str | None = None,
    model: str | None = None,
    status: str | None = None,
    usage_accounted: bool = False,
    request_started_at: datetime | None = None,
) -> ResponseRoute:
    route = await get_response_route(db, response_id, token_id, owner_id=owner_id)
    if route is None:
        route_kwargs = {
            "response_id": response_id,
            "token_id": token_id,
            "owner_id": owner_id,
            "channel_id": channel_id,
        }
        if request_started_at is not None:
            route_kwargs["created_at"] = request_started_at
        route = ResponseRoute(
            **route_kwargs,
        )
        db.add(route)
    route.channel_id = channel_id
    route.conversation_id = conversation_id or route.conversation_id
    route.model = model or route.model
    route.status = status or route.status
    if usage_accounted:
        route.usage_accounted = True
    await db.flush()
    return route
