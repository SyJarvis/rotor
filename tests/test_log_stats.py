import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin.logs import (
    _cache_hit_rate,
    _calendar_time_window,
    _usage_time_window,
    count_logs,
    get_log_stats,
    get_log_timeseries_by_model,
    get_model_usage,
    list_logs,
)
from rotor.application_settings import application_settings
from rotor.database import Base
from rotor.models.log import RequestLog


def _log(*, created_at: datetime, model: str, total_tokens: int) -> RequestLog:
    return RequestLog(
        token_id=1,
        channel_id=1,
        model=model,
        request_model=model,
        prompt_tokens=total_tokens,
        completion_tokens=0,
        total_tokens=total_tokens,
        uncached_input_tokens=total_tokens,
        success=True,
        created_at=created_at,
        latency=1.0,
        ip="127.0.0.1",
    )


def _log_with_cache(
    *,
    created_at: datetime,
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int = 0,
) -> RequestLog:
    return RequestLog(
        token_id=1,
        channel_id=1,
        model=model,
        request_model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        total_tokens=prompt_tokens,
        uncached_input_tokens=(
            prompt_tokens - cached_tokens - cache_write_tokens
        ),
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        success=True,
        created_at=created_at,
        latency=1.0,
        ip="127.0.0.1",
    )


def test_log_stats_respects_days_and_model_filters():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        now = datetime.utcnow()
        async with sessions() as db:
            db.add_all([
                _log(created_at=now - timedelta(days=2), model="recent-a", total_tokens=10),
                _log(created_at=now - timedelta(days=3), model="recent-b", total_tokens=20),
                _log(created_at=now - timedelta(days=10), model="old", total_tokens=1000),
            ])
            await db.commit()

            seven_days = await get_log_stats(
                token_id=None,
                channel_id=None,
                model=None,
                days=7,
                db=db,
            )
            assert seven_days["total_requests"] == 2
            assert seven_days["total_tokens"] == 30

            recent_a = await get_log_stats(
                token_id=None,
                channel_id=None,
                model="recent-a",
                days=7,
                db=db,
            )
            assert recent_a["total_requests"] == 1
            assert recent_a["total_tokens"] == 10

            models = await get_model_usage(
                days=7,
                limit=20,
                channel_id=None,
                model="recent-b",
                db=db,
            )
            assert models == [{
                "model": "recent-b",
                "request_count": 1,
                "total_tokens": 20,
                "prompt_tokens": 20,
                "completion_tokens": 0,
                "uncached_input_tokens": 20,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "usage_v2_requests": 1,
                "total_cost": 0.0,
                "currency": None,
                "cost_totals_by_currency": {},
                "costed_requests": 0,
                "cache_hit_rate": 0.0,
            }]

        await engine.dispose()

    asyncio.run(scenario())

def test_log_stats_includes_cached_tokens():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        now = datetime.utcnow()
        async with sessions() as db:
            db.add_all([
                _log_with_cache(created_at=now - timedelta(hours=1), model="claude",
                                prompt_tokens=100, cached_tokens=80),
                _log_with_cache(created_at=now - timedelta(days=2), model="claude",
                                prompt_tokens=200, cached_tokens=100),
            ])
            await db.commit()

            last_day = await get_log_stats(
                token_id=None, channel_id=None, model=None, days=1, db=db,
            )
            assert last_day["prompt_tokens"] == 100
            assert last_day["cached_tokens"] == 80
            assert last_day["uncached_input_tokens"] == 20
            assert last_day["cache_hit_rate"] == 80.0

            stats = await get_log_stats(
                token_id=None, channel_id=None, model=None, days=7, db=db,
            )
            assert stats["cached_tokens"] == 180
            assert stats["prompt_tokens"] == 300
            assert stats["uncached_input_tokens"] == 120
            assert stats["cache_hit_rate"] == 60.0

            models = await get_model_usage(
                days=7,
                limit=20,
                channel_id=None,
                model="claude",
                db=db,
            )
            assert models[0]["cache_hit_rate"] == 60.0

        await engine.dispose()

    asyncio.run(scenario())


def test_cache_hit_rate_is_bounded_for_legacy_inconsistent_rows() -> None:
    assert _cache_hit_rate(100, 300) == 100.0


def test_cost_stats_do_not_add_different_currencies() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        now = datetime.utcnow()
        async with sessions() as db:
            usd = _log(created_at=now, model="mixed", total_tokens=10)
            usd.cost = 1.25
            usd.currency = "USD"
            usd.cost_status = "calculated"
            cny = _log(created_at=now, model="mixed", total_tokens=10)
            cny.cost = 7.5
            cny.currency = "CNY"
            cny.cost_status = "calculated"
            db.add_all([usd, cny])
            await db.commit()

            stats = await get_log_stats(
                token_id=None,
                channel_id=None,
                model=None,
                days=1,
                db=db,
            )
            models = await get_model_usage(
                days=1,
                limit=20,
                channel_id=None,
                model="mixed",
                db=db,
            )

        await engine.dispose()

        assert stats["total_cost"] is None
        assert stats["currency"] is None
        assert stats["cost_totals_by_currency"] == {
            "CNY": 7.5,
            "USD": 1.25,
        }
        assert stats["costed_requests"] == 2
        assert models[0]["total_cost"] is None
        assert models[0]["cost_totals_by_currency"] == {
            "CNY": 7.5,
            "USD": 1.25,
        }

    asyncio.run(scenario())


def test_usage_queries_respect_an_explicit_day_window():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        start = datetime(2026, 8, 10, 16, 0, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        async with sessions() as db:
            db.add_all([
                _log(created_at=(start - timedelta(seconds=1)).replace(tzinfo=None), model="before", total_tokens=1),
                _log(created_at=start.replace(tzinfo=None), model="selected", total_tokens=10),
                _log(created_at=(end - timedelta(seconds=1)).replace(tzinfo=None), model="selected", total_tokens=20),
                _log(created_at=end.replace(tzinfo=None), model="after", total_tokens=100),
            ])
            await db.commit()

            stats = await get_log_stats(
                token_id=None,
                channel_id=None,
                model=None,
                days=7,
                start_time=start,
                end_time=end,
                db=db,
            )
            assert stats["total_requests"] == 2
            assert stats["total_tokens"] == 30

            models = await get_model_usage(
                days=7,
                limit=20,
                channel_id=None,
                model=None,
                start_time=start,
                end_time=end,
                db=db,
            )
            assert [item["model"] for item in models] == ["selected"]

            timeline = await get_log_timeseries_by_model(
                days=7,
                bucket="hour",
                today=False,
                start_time=start,
                end_time=end,
                channel_id=None,
                model=None,
                db=db,
            )
            assert sum(item["requests"] for item in timeline) == 2
            assert sum(item["tokens"] for item in timeline) == 30

        await engine.dispose()

    asyncio.run(scenario())


def test_usage_date_window_includes_the_complete_selected_day():
    timezone_name = application_settings.get().display_timezone
    display_timezone = ZoneInfo(timezone_name)
    selected = date(2026, 8, 11)

    start, end = _usage_time_window(
        days=7,
        start_time=None,
        end_time=None,
        start_date=selected,
        end_date=selected,
    )

    assert (start, end) == (
        datetime.combine(selected, time.min, display_timezone)
        .astimezone(timezone.utc).replace(tzinfo=None),
        datetime.combine(selected + timedelta(days=1), time.min, display_timezone)
        .astimezone(timezone.utc).replace(tzinfo=None),
    )


def test_log_stats_date_window_includes_both_boundary_dates():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        timezone_name = application_settings.get().display_timezone
        display_timezone = ZoneInfo(timezone_name)
        first_date = date(2026, 8, 11)
        last_date = first_date + timedelta(days=1)
        start = datetime.combine(first_date, time.min, display_timezone).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
        end = datetime.combine(
            last_date + timedelta(days=1), time.min, display_timezone
        ).astimezone(timezone.utc).replace(tzinfo=None)
        async with sessions() as db:
            db.add_all([
                _log(created_at=start - timedelta(seconds=1), model="before", total_tokens=1),
                _log(created_at=start, model="first-date", total_tokens=10),
                _log(created_at=end - timedelta(seconds=1), model="last-date", total_tokens=20),
                _log(created_at=end, model="after", total_tokens=100),
            ])
            await db.commit()

            stats = await get_log_stats(
                token_id=None,
                channel_id=None,
                model=None,
                days=7,
                start_date=first_date,
                end_date=last_date,
                db=db,
            )

        await engine.dispose()

        assert stats["total_requests"] == 2
        assert stats["total_tokens"] == 30

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"start_date": date(2026, 8, 11)}, "must be provided together"),
        (
            {"start_date": date(2026, 8, 12), "end_date": date(2026, 8, 11)},
            "end_date must not be before start_date",
        ),
        (
            {
                "start_date": date.today() + timedelta(days=2),
                "end_date": date.today() + timedelta(days=2),
            },
            "end_date cannot be in the future",
        ),
        (
            {
                "start_date": date(2026, 8, 11),
                "end_date": date(2026, 8, 11),
                "start_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
                "end_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
            },
            "date range cannot be combined",
        ),
        (
            {
                "start_date": date(2026, 8, 11),
                "end_date": date(2026, 8, 11),
                "period": "day",
            },
            "date range cannot be combined",
        ),
    ],
)
def test_usage_date_window_rejects_invalid_combinations(overrides, message):
    arguments = {
        "days": 7,
        "start_time": None,
        "end_time": None,
        "start_date": None,
        "end_date": None,
    }
    arguments.update(overrides)

    with pytest.raises(HTTPException, match=message):
        _usage_time_window(**arguments)


def test_log_list_and_count_keep_all_time_behavior_without_a_window():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions() as db:
            db.add_all([
                _log(created_at=datetime(2020, 1, 1), model="selected", total_tokens=10),
                _log(created_at=datetime.utcnow(), model="other", total_tokens=20),
            ])
            await db.commit()

            logs = await list_logs(
                skip=0,
                limit=100,
                model="selected",
                db=db,
            )
            count = await count_logs(model="selected", db=db)
            assert [item.model for item in logs] == ["selected"]
            assert count == {"count": 1}

        await engine.dispose()

    asyncio.run(scenario())


def test_log_list_and_count_normalize_the_same_explicit_window():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        start = datetime(2026, 8, 11, 8, tzinfo=timezone(timedelta(hours=8)))
        end = start + timedelta(hours=24)
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
        async with sessions() as db:
            db.add_all([
                _log(created_at=start_utc - timedelta(seconds=1), model="before", total_tokens=1),
                _log(created_at=start_utc, model="first", total_tokens=10),
                _log(created_at=end_utc - timedelta(seconds=1), model="last", total_tokens=20),
                _log(created_at=end_utc, model="after", total_tokens=100),
            ])
            await db.commit()

            logs = await list_logs(
                skip=0,
                limit=100,
                start_time=start,
                end_time=end,
                db=db,
            )
            count = await count_logs(start_time=start, end_time=end, db=db)

        await engine.dispose()

        assert {item.model for item in logs} == {"first", "last"}
        assert count == {"count": 2}

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", [list_logs, count_logs])
def test_log_list_and_count_reject_incomplete_explicit_window(endpoint):
    async def scenario():
        with pytest.raises(HTTPException, match="must be provided together"):
            await endpoint(
                **({"skip": 0, "limit": 100} if endpoint is list_logs else {}),
                start_time=datetime(2026, 8, 11, tzinfo=timezone.utc),
                end_time=None,
                db=None,
            )

    asyncio.run(scenario())


def test_calendar_windows_use_configured_timezone_boundaries():
    day_start, day_end = _calendar_time_window(
        "day",
        date(2026, 8, 11),
        "Asia/Shanghai",
    )
    week_start, week_end = _calendar_time_window(
        "week",
        date(2026, 8, 12),
        "Asia/Shanghai",
    )
    month_start, month_end = _calendar_time_window(
        "month",
        date(2026, 8, 12),
        "Asia/Shanghai",
    )

    assert (day_start, day_end) == (
        datetime(2026, 8, 10, 16),
        datetime(2026, 8, 11, 16),
    )
    assert (week_start, week_end) == (
        datetime(2026, 8, 9, 16),
        datetime(2026, 8, 16, 16),
    )
    assert (month_start, month_end) == (
        datetime(2026, 7, 31, 16),
        datetime(2026, 8, 31, 16),
    )


def test_calendar_day_window_keeps_local_midnight_across_dst():
    start, end = _calendar_time_window(
        "day",
        date(2026, 3, 8),
        "America/New_York",
    )

    assert (start, end) == (
        datetime(2026, 3, 8, 5),
        datetime(2026, 3, 9, 4),
    )
