from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.response_route import ResponseRoute


async def get_response_route(
    db: AsyncSession,
    response_id: str,
    token_id: int,
) -> ResponseRoute | None:
    result = await db.execute(
        select(ResponseRoute).where(
            ResponseRoute.response_id == response_id,
            ResponseRoute.token_id == token_id,
        )
    )
    return result.scalar_one_or_none()


async def save_response_route(
    db: AsyncSession,
    *,
    response_id: str,
    token_id: int,
    channel_id: int,
    conversation_id: str | None = None,
    model: str | None = None,
    status: str | None = None,
    usage_accounted: bool = False,
) -> ResponseRoute:
    result = await db.execute(
        select(ResponseRoute).where(
            ResponseRoute.response_id == response_id,
            ResponseRoute.token_id == token_id,
        )
    )
    route = result.scalar_one_or_none()
    if route is None:
        route = ResponseRoute(
            response_id=response_id,
            token_id=token_id,
            channel_id=channel_id,
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
