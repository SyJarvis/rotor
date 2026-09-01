from typing import List, Literal, Optional
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, case, func

from rotor.config import settings
from rotor.application_settings import application_settings
from rotor.database import get_db
from rotor.models.log import RequestLog

router = APIRouter(prefix="/logs", tags=["logs"])


def _cache_hit_rate(prompt_tokens: int | None, cached_tokens: int | None) -> float:
    prompt = int(prompt_tokens or 0)
    if prompt <= 0:
        return 0.0
    return min(max(int(cached_tokens or 0), 0) / prompt * 100, 100.0)


def _cost_summary(
    rows,
) -> tuple[float | None, str | None, dict[str, float], int]:
    totals = {
        row.currency: float(row.total_cost or 0.0)
        for row in rows
        if row.currency
    }
    costed_requests = sum(int(row.costed_requests or 0) for row in rows)
    if not totals:
        return 0.0, None, {}, costed_requests
    if len(totals) == 1:
        currency, total = next(iter(totals.items()))
        return total, currency, totals, costed_requests
    return None, None, totals, costed_requests


def _usage_time_window(
    *,
    days: int,
    start_time: datetime | None,
    end_time: datetime | None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
) -> tuple[datetime, datetime | None]:
    if period is not None:
        if start_time is not None or end_time is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="period cannot be combined with start_time or end_time",
            )
        return _calendar_time_window(period, period_date)
    if start_time is None and end_time is None:
        return datetime.utcnow() - timedelta(days=days), None
    if start_time is None or end_time is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_time and end_time must be provided together",
        )

    def utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    start = utc_naive(start_time)
    end = utc_naive(end_time)
    if start >= end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_time must be after start_time",
        )
    return start, end


def _calendar_time_window(
    period: Literal["day", "week", "month"],
    period_date: date | None,
    display_timezone_name: str | None = None,
) -> tuple[datetime, datetime]:
    timezone_name = display_timezone_name or application_settings.get().display_timezone
    display_timezone = ZoneInfo(timezone_name)
    anchor = period_date or datetime.now(display_timezone).date()
    if period == "day":
        start_date = anchor
        end_date = anchor + timedelta(days=1)
    elif period == "week":
        start_date = anchor - timedelta(days=anchor.weekday())
        end_date = start_date + timedelta(days=7)
    else:
        start_date = anchor.replace(day=1)
        end_date = (
            start_date.replace(year=start_date.year + 1, month=1)
            if start_date.month == 12
            else start_date.replace(month=start_date.month + 1)
        )

    def utc_naive(day: date) -> datetime:
        return datetime.combine(day, time.min, display_timezone).astimezone(
            timezone.utc
        ).replace(tzinfo=None)

    return utc_naive(start_date), utc_naive(end_date)


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
    uncached_input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    usage_schema_version: str
    capacity_snapshot: Optional[dict] = None
    cache_scope: Optional[str]
    capacity_scope: Optional[str]
    billing_scope: Optional[str]
    cost: Optional[float]
    currency: str
    cost_status: str
    tariff_version: Optional[str]
    tariff_period: Optional[str]
    tariff_snapshot: Optional[dict] = None
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


def _log_filter_conditions(
    *,
    token_id: int | None = None,
    channel_id: int | None = None,
    model: str | None = None,
    success: bool | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> list:
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
        conditions.append(RequestLog.created_at < end_time)
    return conditions


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
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
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
    if period is not None:
        start_time, end_time = _usage_time_window(
            days=1,
            start_time=start_time,
            end_time=end_time,
            period=period,
            period_date=period_date,
        )
    conditions = _log_filter_conditions(
        token_id=token_id,
        channel_id=channel_id,
        model=model,
        success=success,
        start_time=start_time,
        end_time=end_time,
    )

    query = select(RequestLog)

    if conditions:
        query = query.where(and_(*conditions))

    query = query.order_by(RequestLog.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    logs = result.scalars().all()

    return logs


@router.get("/count")
async def count_logs(
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    success: Optional[bool] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Count request logs matching the list filters."""
    if period is not None:
        start_time, end_time = _usage_time_window(
            days=1,
            start_time=start_time,
            end_time=end_time,
            period=period,
            period_date=period_date,
        )
    conditions = _log_filter_conditions(
        token_id=token_id,
        channel_id=channel_id,
        model=model,
        success=success,
        start_time=start_time,
        end_time=end_time,
    )
    query = select(func.count(RequestLog.id))
    if conditions:
        query = query.where(and_(*conditions))
    result = await db.execute(query)
    return {"count": result.scalar() or 0}


@router.get("/stats")
async def get_log_stats(
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    days: int = Query(7, ge=1, le=90),
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
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
    start_time, end_time = _usage_time_window(
        days=days,
        start_time=start_time,
        end_time=end_time,
        period=period,
        period_date=period_date,
    )

    # Build conditions
    conditions = [RequestLog.created_at >= start_time]
    if end_time is not None:
        conditions.append(RequestLog.created_at < end_time)

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
            func.sum(RequestLog.uncached_input_tokens).label('uncached_input_tokens'),
            func.sum(RequestLog.cached_tokens).label('cached_tokens'),
            func.sum(RequestLog.cache_write_tokens).label('cache_write_tokens'),
            func.sum(RequestLog.cache_write_5m_tokens).label('cache_write_5m_tokens'),
            func.sum(RequestLog.cache_write_1h_tokens).label('cache_write_1h_tokens'),
            func.sum(case(
                (RequestLog.usage_schema_version == "2", 1),
                else_=0,
            )).label('usage_v2_requests'),
            func.avg(RequestLog.latency).label('avg_latency'),
        )
        .where(and_(*conditions))
    )

    row = result.one()

    cost_rows = (await db.execute(
        select(
            RequestLog.currency,
            func.sum(RequestLog.cost).label("total_cost"),
            func.count(RequestLog.id).label("costed_requests"),
        )
        .where(and_(
            *conditions,
            RequestLog.cost_status == "calculated",
        ))
        .group_by(RequestLog.currency)
    )).all()
    total_cost, currency, cost_totals, costed_requests = _cost_summary(
        cost_rows
    )

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
        "uncached_input_tokens": row.uncached_input_tokens or 0,
        "cached_tokens": row.cached_tokens or 0,
        "cache_write_tokens": row.cache_write_tokens or 0,
        "cache_write_5m_tokens": row.cache_write_5m_tokens or 0,
        "cache_write_1h_tokens": row.cache_write_1h_tokens or 0,
        "usage_v2_requests": row.usage_v2_requests or 0,
        "cache_hit_rate": _cache_hit_rate(
            row.prompt_tokens,
            row.cached_tokens,
        ),
        "total_cost": total_cost,
        "currency": currency,
        "cost_totals_by_currency": cost_totals,
        "costed_requests": costed_requests,
        "avg_latency": float(row.avg_latency or 0),
    }


@router.get("/timeseries")
async def get_log_timeseries(
    days: int = Query(7, ge=1, le=90),
    bucket: str = Query("auto", pattern="^(auto|hour|day)$"),
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    success: Optional[bool] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
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
    start_time, end_time = _usage_time_window(
        days=days,
        start_time=start_time,
        end_time=end_time,
        period=period,
        period_date=period_date,
    )

    conditions = [RequestLog.created_at >= start_time]
    if end_time is not None:
        conditions.append(RequestLog.created_at < end_time)
    if token_id is not None:
        conditions.append(RequestLog.token_id == token_id)
    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)
    if model is not None:
        conditions.append(RequestLog.model == model)
    if success is not None:
        conditions.append(RequestLog.success == success)

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
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
    channel_id: Optional[int] = None,
    model: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Time-bucketed token usage broken down by model — for stacked area charts.

    When today=True, start is aligned to midnight UTC of the current day
    (useful for "today 0:00 → now" views). Otherwise the window is `days` days back.
    """
    resolved_bucket = "hour" if (bucket == "auto" and days <= 3) or bucket == "hour" else "day"
    if period is not None:
        start_time, end_time = _usage_time_window(
            days=days,
            start_time=start_time,
            end_time=end_time,
            period=period,
            period_date=period_date,
        )
    elif today and start_time is None and end_time is None:
        now = datetime.utcnow()
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = None
    else:
        start_time, end_time = _usage_time_window(
            days=days,
            start_time=start_time,
            end_time=end_time,
        )

    conditions = [RequestLog.created_at >= start_time]
    if end_time is not None:
        conditions.append(RequestLog.created_at < end_time)
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
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    period: Literal["day", "week", "month"] | None = None,
    period_date: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get model usage statistics."""
    start_time, end_time = _usage_time_window(
        days=days,
        start_time=start_time,
        end_time=end_time,
        period=period,
        period_date=period_date,
    )
    conditions = [RequestLog.created_at >= start_time]
    if end_time is not None:
        conditions.append(RequestLog.created_at < end_time)
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
            func.sum(RequestLog.uncached_input_tokens).label('uncached_input_tokens'),
            func.sum(RequestLog.cached_tokens).label('cached_tokens'),
            func.sum(RequestLog.cache_write_tokens).label('cache_write_tokens'),
            func.sum(RequestLog.cache_write_5m_tokens).label('cache_write_5m_tokens'),
            func.sum(RequestLog.cache_write_1h_tokens).label('cache_write_1h_tokens'),
            func.sum(case(
                (RequestLog.usage_schema_version == "2", 1),
                else_=0,
            )).label('usage_v2_requests'),
        )
        .where(and_(*conditions))
        .group_by(RequestLog.model)
        .order_by(func.count(RequestLog.id).desc())
        .limit(limit)
    )

    rows = result.all()
    selected_models = [row.model for row in rows]
    cost_rows = []
    if selected_models:
        cost_rows = (await db.execute(
            select(
                RequestLog.model,
                RequestLog.currency,
                func.sum(RequestLog.cost).label("total_cost"),
                func.count(RequestLog.id).label("costed_requests"),
            )
            .where(and_(
                *conditions,
                RequestLog.model.in_(selected_models),
                RequestLog.cost_status == "calculated",
            ))
            .group_by(RequestLog.model, RequestLog.currency)
        )).all()
    costs_by_model = {
        selected_model: _cost_summary([
            cost_row
            for cost_row in cost_rows
            if cost_row.model == selected_model
        ])
        for selected_model in selected_models
    }

    return [
        {
            "model": row.model,
            "request_count": row.request_count,
            "total_tokens": row.total_tokens or 0,
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "uncached_input_tokens": row.uncached_input_tokens or 0,
            "cached_tokens": row.cached_tokens or 0,
            "cache_write_tokens": row.cache_write_tokens or 0,
            "cache_write_5m_tokens": row.cache_write_5m_tokens or 0,
            "cache_write_1h_tokens": row.cache_write_1h_tokens or 0,
            "usage_v2_requests": row.usage_v2_requests or 0,
            "total_cost": costs_by_model[row.model][0],
            "currency": costs_by_model[row.model][1],
            "cost_totals_by_currency": costs_by_model[row.model][2],
            "costed_requests": costs_by_model[row.model][3],
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
