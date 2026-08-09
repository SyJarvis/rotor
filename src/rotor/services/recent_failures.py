import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.request_attempt import RequestAttempt
from rotor.schemas.control import (
    FailureGroupKey,
    RecentFailureGroup,
)


FailureGroupBy = Literal["model", "channel", "category", "status"]


@dataclass(frozen=True)
class RecentFailuresPage:
    groups: list[RecentFailureGroup]
    next_cursor: str | None
    has_more: bool


async def list_recent_failures(
    db: AsyncSession,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    model: str | None = None,
    channel_id: int | None = None,
    category: str | None = None,
    group_by: FailureGroupBy = "category",
    cursor: str | None = None,
    limit: int = 50,
    excluded_agent_run_id: str | None = None,
) -> RecentFailuresPage:
    filter_key = _filter_key(
        model=model,
        channel_id=channel_id,
        category=category,
        group_by=group_by,
        limit=limit,
        excluded_agent_run_id=excluded_agent_run_id,
    )
    if cursor is None:
        effective_end = _as_utc(end_time) if end_time else datetime.now(
            timezone.utc
        )
        effective_start = (
            _as_utc(start_time)
            if start_time
            else effective_end - timedelta(hours=1)
        )
        offset = 0
    else:
        offset, effective_start, effective_end = _decode_cursor(
            cursor,
            expected_filter_key=filter_key,
        )
        if start_time and _as_utc(start_time) != effective_start:
            raise ValueError("cursor does not match start_time")
        if end_time and _as_utc(end_time) != effective_end:
            raise ValueError("cursor does not match end_time")

    _validate_window(effective_start, effective_end)
    group_column = _group_column(group_by)
    predicates = [
        RequestAttempt.outcome == "failed",
        RequestAttempt.started_at >= effective_start,
        RequestAttempt.started_at <= effective_end,
    ]
    if model is not None:
        predicates.append(RequestAttempt.requested_model == model)
    if channel_id is not None:
        predicates.append(RequestAttempt.channel_id == channel_id)
    if category is not None:
        predicates.append(RequestAttempt.error_category == category)
    if excluded_agent_run_id is not None:
        predicates.append(
            ~(
                (RequestAttempt.request_origin == "rotor_agent")
                & (
                    RequestAttempt.agent_run_id
                    == excluded_agent_run_id
                )
            )
        )

    grouped = (
        select(
            group_column.label("group_value"),
            func.count().label("failure_count"),
            func.max(RequestAttempt.started_at).label("latest_at"),
        )
        .where(*predicates)
        .group_by(group_column)
        .order_by(
            func.max(RequestAttempt.started_at).desc(),
            group_column,
        )
        .offset(offset)
        .limit(limit + 1)
    )
    rows = list((await db.execute(grouped)).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    samples = await _sample_request_ids(
        db,
        predicates=predicates,
        group_column=group_column,
        group_values=[row.group_value for row in page_rows],
    )
    groups = [
        RecentFailureGroup(
            group=_group_key(group_by, row.group_value),
            count=row.failure_count,
            latest_at=row.latest_at,
            sample_request_ids=samples.get(row.group_value, []),
        )
        for row in page_rows
    ]
    next_cursor = (
        _encode_cursor(
            offset=offset + limit,
            start_time=effective_start,
            end_time=effective_end,
            filter_key=filter_key,
        )
        if has_more
        else None
    )
    return RecentFailuresPage(
        groups=groups,
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def _sample_request_ids(
    db: AsyncSession,
    *,
    predicates: list,
    group_column,
    group_values: list,
) -> dict[object, list[str]]:
    if not group_values:
        return {}
    non_null_values = [
        value for value in group_values if value is not None
    ]
    group_filter = group_column.in_(non_null_values)
    if None in group_values:
        group_filter = or_(group_filter, group_column.is_(None))
    latest_requests = (
        select(
            group_column.label("group_value"),
            RequestAttempt.request_id.label("request_id"),
            func.max(RequestAttempt.started_at).label("latest_at"),
        )
        .where(*predicates, group_filter)
        .group_by(group_column, RequestAttempt.request_id)
        .subquery()
    )
    ranked = select(
        latest_requests.c.group_value,
        latest_requests.c.request_id,
        func.row_number()
        .over(
            partition_by=latest_requests.c.group_value,
            order_by=latest_requests.c.latest_at.desc(),
        )
        .label("row_number"),
    ).subquery()
    rows = (
        await db.execute(
            select(ranked.c.group_value, ranked.c.request_id)
            .where(ranked.c.row_number <= 3)
            .order_by(ranked.c.group_value, ranked.c.row_number)
        )
    ).all()
    samples: dict[object, list[str]] = {}
    for row in rows:
        samples.setdefault(row.group_value, []).append(row.request_id)
    return samples


def _group_column(group_by: FailureGroupBy):
    return {
        "model": RequestAttempt.requested_model,
        "channel": RequestAttempt.channel_id,
        "category": func.coalesce(
            RequestAttempt.error_category,
            "unknown",
        ),
        "status": RequestAttempt.upstream_status,
    }[group_by]


def _group_key(
    group_by: FailureGroupBy,
    value: object,
) -> FailureGroupKey:
    if group_by == "model":
        return FailureGroupKey(model=str(value))
    if group_by == "channel":
        return FailureGroupKey(channel_id=int(value))
    if group_by == "category":
        return FailureGroupKey(category=str(value))
    return FailureGroupKey(
        status=int(value) if value is not None else None
    )


def _validate_window(start_time: datetime, end_time: datetime) -> None:
    if start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")
    if end_time - start_time > timedelta(days=7):
        raise ValueError("failure query window must not exceed 7 days")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _filter_key(**filters: object) -> str:
    encoded = json.dumps(
        filters,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(
    *,
    offset: int,
    start_time: datetime,
    end_time: datetime,
    filter_key: str,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "offset": offset,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "filter_key": filter_key,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    expected_filter_key: str,
) -> tuple[int, datetime, datetime]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + padding)
        )
        offset = payload["offset"]
        start_time = datetime.fromisoformat(payload["start_time"])
        end_time = datetime.fromisoformat(payload["end_time"])
        filter_key = payload["filter_key"]
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("invalid failure cursor") from exc
    if (
        payload.get("v") != 1
        or not isinstance(offset, int)
        or offset < 0
        or filter_key != expected_filter_key
    ):
        raise ValueError("invalid or mismatched failure cursor")
    return offset, _as_utc(start_time), _as_utc(end_time)
