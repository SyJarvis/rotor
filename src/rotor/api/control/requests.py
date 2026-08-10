from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.control_auth import (
    ActorContext,
    ControlAPIException,
    control_request_id,
    require_control_scope,
)
from rotor.database import get_db
from rotor.schemas.error import ErrorCategory
from rotor.schemas.control import (
    ControlPage,
    ControlResponseMeta,
    ControlTimeWindow,
    ModelUsageControlResponse,
    RecentFailuresControlResponse,
    RequestTraceControlResponse,
)
from rotor.services.model_usage import list_model_usage
from rotor.services.recent_failures import list_recent_failures
from rotor.services.request_traces import get_request_trace


router = APIRouter(tags=["control-requests"])


@router.get(
    "/usage/models",
    response_model=ModelUsageControlResponse,
)
async def read_model_usage(
    request: Request,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _: ActorContext = Depends(require_control_scope("usage:read")),
    db: AsyncSession = Depends(get_db),
) -> ModelUsageControlResponse:
    try:
        result = await list_model_usage(
            db,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
    except ValueError as exc:
        raise ControlAPIException(
            status_code=400,
            code="invalid_model_usage_query",
            message=str(exc),
        ) from exc
    return ModelUsageControlResponse(
        request_id=control_request_id(request),
        data=result.items,
        window=ControlTimeWindow(
            start_time=result.start_time,
            end_time=result.end_time,
        ),
    )


@router.get(
    "/failures",
    response_model=RecentFailuresControlResponse,
)
async def read_recent_failures(
    request: Request,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    model: str | None = Query(default=None, max_length=200),
    channel_id: int | None = Query(default=None, ge=1),
    category: ErrorCategory | None = None,
    group_by: Literal[
        "model",
        "channel",
        "category",
        "status",
    ] = "category",
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorContext = Depends(
        require_control_scope("request_trace:read")
    ),
    db: AsyncSession = Depends(get_db),
) -> RecentFailuresControlResponse:
    try:
        page = await list_recent_failures(
            db,
            start_time=start_time,
            end_time=end_time,
            model=model,
            channel_id=channel_id,
            category=category,
            group_by=group_by,
            cursor=cursor,
            limit=limit,
            excluded_agent_run_id=actor.agent_run_id,
        )
    except ValueError as exc:
        raise ControlAPIException(
            status_code=400,
            code="invalid_failure_query",
            message=str(exc),
        ) from exc
    return RecentFailuresControlResponse(
        request_id=control_request_id(request),
        data=page.groups,
        page=ControlPage(
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            limit=limit,
        ),
    )


@router.get(
    "/requests/{request_id}/trace",
    response_model=RequestTraceControlResponse,
)
async def read_request_trace(
    request: Request,
    request_id: str = Path(min_length=1, max_length=64),
    actor: ActorContext = Depends(
        require_control_scope("request_trace:read")
    ),
    db: AsyncSession = Depends(get_db),
) -> RequestTraceControlResponse:
    trace = await get_request_trace(db, request_id)
    if trace is None or (
        actor.agent_run_id is not None
        and trace.origin == "rotor_agent"
        and trace.agent_run_id == actor.agent_run_id
    ):
        raise ControlAPIException(
            status_code=404,
            code="request_trace_not_found",
            message="Request trace not found",
        )

    redactions = [
        f"data.attempts[{index}].error.sanitized_body"
        for index, attempt in enumerate(trace.attempts)
        if (
            attempt.error is not None
            and attempt.error.sanitized_body is not None
        )
    ]
    return RequestTraceControlResponse(
        request_id=control_request_id(request),
        data=trace,
        meta=ControlResponseMeta(redactions=redactions),
    )
