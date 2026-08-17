import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin.logs import (
    _calendar_time_window,
    count_logs,
    get_log_stats,
    get_log_timeseries_by_model,
    get_model_usage,
)
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
        success=True,
        created_at=created_at,
        latency=1.0,
        ip="127.0.0.1",
    )


def _log_with_cache(*, created_at: datetime, model: str, prompt_tokens: int, cached_tokens: int) -> RequestLog:
    return RequestLog(
        token_id=1,
        channel_id=1,
        model=model,
        request_model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        total_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
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
                "cached_tokens": 0,
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
            assert last_day["cache_hit_rate"] == 80.0

            stats = await get_log_stats(
                token_id=None, channel_id=None, model=None, days=7, db=db,
            )
            assert stats["cached_tokens"] == 180
            assert stats["prompt_tokens"] == 300
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


def test_log_count_respects_list_filters():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions() as db:
            db.add_all([
                _log(created_at=datetime.utcnow(), model="selected", total_tokens=10),
                _log(created_at=datetime.utcnow(), model="other", total_tokens=20),
            ])
            await db.commit()

            count = await count_logs(model="selected", db=db)
            assert count == {"count": 1}

        await engine.dispose()

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
