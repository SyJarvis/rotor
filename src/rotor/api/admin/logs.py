from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, case, func

from rotor.config import settings
from rotor.database import get_db
from rotor.models.log import RequestLog

router = APIRouter(prefix="/logs", tags=["logs"])


def _cache_hit_rate(prompt_tokens: int | None, cached_tokens: int | None) -> float:
    prompt = int(prompt_tokens or 0)
    if prompt <= 0:
        return 0.0
    return int(cached_tokens or 0) / prompt * 100


class RequestLogResponse(BaseModel):
    """Schema for request log response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    token_id: Optional[int]
    channel_id: Optional[int]
    model: str
    request_model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cost: Optional[float]
    success: bool
    error_code: Optional[str]
    error_message: Optional[str]
    latency: Optional[float]
    created_at: datetime
    ip: Optional[str]


class RequestLogDetailResponse(RequestLogResponse):
    """Detail view including request/response bodies for debugging."""

    request_body: Optional[dict] = None
    response_body: Optional[dict] = None


@router.get("", response_model=List[RequestLogResponse])
async def list_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    success: Optional[bool] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    List request logs with optional filters.

    Args:
        skip: Number of records to skip
        limit: Maximum number of records to return
        token_id: Filter by token ID
        channel_id: Filter by channel ID
        model: Filter by model name
        success: Filter by success status
        start_time: Filter logs after this time
        end_time: Filter logs before this time
    """
    # Build query with filters
    conditions = []

    if token_id is not None:
        conditions.append(RequestLog.token_id == token_id)

    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)

    if model is not None:
        conditions.append(RequestLog.model == model)

    if success is not None:
        conditions.append(RequestLog.success == success)

    if start_time is not None:
        conditions.append(RequestLog.created_at >= start_time)

    if end_time is not None:
        conditions.append(RequestLog.created_at <= end_time)

    query = select(RequestLog)

    if conditions:
        query = query.where(and_(*conditions))

    query = query.order_by(RequestLog.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    logs = result.scalars().all()

    return logs


@router.get("/stats")
async def get_log_stats(
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """
    Get usage statistics.

    Args:
        token_id: Filter by token ID
        channel_id: Filter by channel ID
        model: Filter by model name
        days: Number of days to include in stats
    """
    start_time = datetime.utcnow() - timedelta(days=days)

    # Build conditions
    conditions = [RequestLog.created_at >= start_time]

    if token_id is not None:
        conditions.append(RequestLog.token_id == token_id)

    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)

    if model is not None:
        conditions.append(RequestLog.model == model)

    # Query stats
    result = await db.execute(
        select(
            func.count(RequestLog.id).label('total_requests'),
            func.sum(RequestLog.total_tokens).label('total_tokens'),
            func.sum(RequestLog.prompt_tokens).label('prompt_tokens'),
            func.sum(RequestLog.completion_tokens).label('completion_tokens'),
            func.sum(RequestLog.cached_tokens).label('cached_tokens'),
            func.sum(RequestLog.cost).label('total_cost'),
            func.avg(RequestLog.latency).label('avg_latency'),
        )
        .where(and_(*conditions))
    )

    row = result.one()

    # Get failed count separately
    failed_result = await db.execute(
        select(func.count(RequestLog.id))
        .where(and_(*conditions, RequestLog.success == False))
    )
    failed_count = failed_result.scalar() or 0

    return {
        "period_days": days,
        "total_requests": row.total_requests or 0,
        "success_requests": (row.total_requests or 0) - failed_count,
        "failed_requests": failed_count,
        "total_tokens": row.total_tokens or 0,
        "prompt_tokens": row.prompt_tokens or 0,
        "completion_tokens": row.completion_tokens or 0,
        "cached_tokens": row.cached_tokens or 0,
        "cache_hit_rate": _cache_hit_rate(
            row.prompt_tokens,
            row.cached_tokens,
        ),
        "total_cost": float(row.total_cost or 0),
        "avg_latency": float(row.avg_latency or 0),
    }


@router.get("/timeseries")
async def get_log_timeseries(
    days: int = Query(7, ge=1, le=90),
    bucket: str = Query("auto", pattern="^(auto|hour|day)$"),
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get time-bucketed usage statistics for charts.

    Args:
        days: Number of days to include.
        bucket: Bucket size. "auto" picks hour for <=3 days, day otherwise.
        token_id: Filter by token ID.
        channel_id: Filter by channel ID.
        model: Filter by model name.
    """
    resolved_bucket = "hour" if (bucket == "auto" and days <= 3) or bucket == "hour" else "day"
    start_time = datetime.utcnow() - timedelta(days=days)

    conditions = [RequestLog.created_at >= start_time]
    if token_id is not None:
        conditions.append(RequestLog.token_id == token_id)
    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)
    if model is not None:
        conditions.append(RequestLog.model == model)

    is_sqlite = "sqlite" in settings.DATABASE_URL
    if is_sqlite:
        fmt = "%Y-%m-%dT%H:00:00" if resolved_bucket == "hour" else "%Y-%m-%d"
        bucket_expr = func.strftime(fmt, RequestLog.created_at)
    else:
        bucket_expr = func.date_trunc(resolved_bucket, RequestLog.created_at)

    result = await db.execute(
        select(
            bucket_expr.label("bucket"),
            func.count(RequestLog.id).label("requests"),
            func.sum(RequestLog.total_tokens).label("tokens"),
            func.sum(RequestLog.success).label("success_count"),
            func.avg(RequestLog.latency).label("avg_latency"),
        )
        .where(and_(*conditions))
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )
    rows = result.all()

    return [
        {
            "bucket": row.bucket,
            "requests": row.requests or 0,
            "tokens": int(row.tokens or 0),
            "success": int(row.success_count or 0),
            "failed": (row.requests or 0) - int(row.success_count or 0),
            "avg_latency": float(row.avg_latency or 0),
        }
        for row in rows
    ]


@router.get("/timeseries_by_model")
async def get_log_timeseries_by_model(
    days: int = Query(7, ge=1, le=90),
    bucket: str = Query("auto", pattern="^(auto|hour|day)$"),
    today: bool = Query(False),
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Time-bucketed token usage broken down by model — for stacked area charts.

    When today=True, start is aligned to midnight UTC of the current day
    (useful for "today 0:00 → now" views). Otherwise the window is `days` days back.
    """
    resolved_bucket = "hour" if (bucket == "auto" and days <= 3) or bucket == "hour" else "day"
    if today:
        now = datetime.utcnow()
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_time = datetime.utcnow() - timedelta(days=days)

    conditions = [RequestLog.created_at >= start_time]
    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)
    if model is not None:
        conditions.append(RequestLog.model == model)

    is_sqlite = "sqlite" in settings.DATABASE_URL
    if is_sqlite:
        fmt = "%Y-%m-%dT%H:00:00" if resolved_bucket == "hour" else "%Y-%m-%d"
        bucket_expr = func.strftime(fmt, RequestLog.created_at)
    else:
        bucket_expr = func.date_trunc(resolved_bucket, RequestLog.created_at)

    result = await db.execute(
        select(
            bucket_expr.label("bucket"),
            RequestLog.model.label("model"),
            func.sum(RequestLog.total_tokens).label("tokens"),
            func.count(RequestLog.id).label("requests"),
        )
        .where(and_(*conditions))
        .group_by(bucket_expr, RequestLog.model)
        .order_by(bucket_expr, RequestLog.model)
    )
    rows = result.all()

    return [
        {
            "bucket": row.bucket,
            "model": row.model,
            "tokens": int(row.tokens or 0),
            "requests": row.requests or 0,
        }
        for row in rows
    ]


@router.get("/models")
async def get_model_usage(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get model usage statistics."""
    start_time = datetime.utcnow() - timedelta(days=days)
    conditions = [RequestLog.created_at >= start_time]
    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)
    if model is not None:
        conditions.append(RequestLog.model == model)

    # Query model usage
    result = await db.execute(
        select(
            RequestLog.model,
            func.count(RequestLog.id).label('request_count'),
            func.sum(RequestLog.total_tokens).label('total_tokens'),
            func.sum(RequestLog.prompt_tokens).label('prompt_tokens'),
            func.sum(RequestLog.completion_tokens).label('completion_tokens'),
            func.sum(RequestLog.cached_tokens).label('cached_tokens'),
        )
        .where(and_(*conditions))
        .group_by(RequestLog.model)
        .order_by(func.count(RequestLog.id).desc())
        .limit(limit)
    )

    rows = result.all()

    return [
        {
            "model": row.model,
            "request_count": row.request_count,
            "total_tokens": row.total_tokens or 0,
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "cached_tokens": row.cached_tokens or 0,
            "cache_hit_rate": _cache_hit_rate(
                row.prompt_tokens,
                row.cached_tokens,
            ),
        }
        for row in rows
    ]


@router.get("/{log_id}", response_model=RequestLogDetailResponse)
async def get_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get full request log detail (including request/response bodies)."""
    result = await db.execute(select(RequestLog).where(RequestLog.id == log_id))
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log {log_id} not found",
        )
    return log
