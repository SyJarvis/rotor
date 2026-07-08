from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, case, func

from rotor.database import get_db
from rotor.models.log import RequestLog

router = APIRouter(prefix="/logs", tags=["logs"])


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
    cost: Optional[float]
    success: bool
    error_code: Optional[str]
    error_message: Optional[str]
    latency: Optional[float]
    created_at: datetime
    ip: Optional[str]


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
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """
    Get usage statistics.

    Args:
        token_id: Filter by token ID
        channel_id: Filter by channel ID
        days: Number of days to include in stats
    """
    start_time = datetime.utcnow() - timedelta(days=days)

    # Build conditions
    conditions = [RequestLog.created_at >= start_time]

    if token_id is not None:
        conditions.append(RequestLog.token_id == token_id)

    if channel_id is not None:
        conditions.append(RequestLog.channel_id == channel_id)

    # Query stats
    result = await db.execute(
        select(
            func.count(RequestLog.id).label('total_requests'),
            func.sum(RequestLog.total_tokens).label('total_tokens'),
            func.sum(RequestLog.prompt_tokens).label('prompt_tokens'),
            func.sum(RequestLog.completion_tokens).label('completion_tokens'),
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
        "total_cost": float(row.total_cost or 0),
        "avg_latency": float(row.avg_latency or 0),
    }


@router.get("/models")
async def get_model_usage(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get model usage statistics."""
    start_time = datetime.utcnow() - timedelta(days=days)

    # Query model usage
    result = await db.execute(
        select(
            RequestLog.model,
            func.count(RequestLog.id).label('request_count'),
            func.sum(RequestLog.total_tokens).label('total_tokens'),
        )
        .where(RequestLog.created_at >= start_time)
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
        }
        for row in rows
    ]
