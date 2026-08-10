import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin.logs import get_log_stats, get_model_usage
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
