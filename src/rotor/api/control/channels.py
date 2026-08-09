from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.control_auth import (
    ActorContext,
    ControlAPIException,
    control_request_id,
    require_control_scope,
)
from rotor.database import get_db
from rotor.schemas.control import (
    ChannelControlResponse,
    ChannelListControlResponse,
    ControlPage,
)
from rotor.services.channels import (
    get_control_channel,
    list_control_channels,
)


router = APIRouter(tags=["control-channels"])


@router.get(
    "/channels",
    response_model=ChannelListControlResponse,
)
async def read_channels(
    request: Request,
    enabled: bool | None = None,
    model: str | None = Query(default=None, max_length=200),
    protocol: str | None = Query(default=None, max_length=50),
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=50, ge=1, le=100),
    _: ActorContext = Depends(require_control_scope("channel:read")),
    db: AsyncSession = Depends(get_db),
) -> ChannelListControlResponse:
    try:
        page = await list_control_channels(
            db,
            enabled=enabled,
            model=model,
            protocol=protocol,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise ControlAPIException(
            status_code=400,
            code="invalid_channel_query",
            message=str(exc),
        ) from exc
    return ChannelListControlResponse(
        request_id=control_request_id(request),
        data=page.items,
        page=ControlPage(
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            limit=limit,
        ),
    )


@router.get(
    "/channels/{channel_id}",
    response_model=ChannelControlResponse,
)
async def read_channel(
    request: Request,
    channel_id: int = Path(ge=1),
    _: ActorContext = Depends(require_control_scope("channel:read")),
    db: AsyncSession = Depends(get_db),
) -> ChannelControlResponse:
    channel = await get_control_channel(db, channel_id)
    if channel is None:
        raise ControlAPIException(
            status_code=404,
            code="channel_not_found",
            message="Channel not found",
        )
    return ChannelControlResponse(
        request_id=control_request_id(request),
        data=channel,
    )
