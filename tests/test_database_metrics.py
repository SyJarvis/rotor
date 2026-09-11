import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import rotor.observability as observations
from rotor.database import _MeasuredQueuePool, _instrument_engine, create_database_engine


def test_connection_acquisition_measures_real_queue_wait_and_timeout(tmp_path, monkeypatch):
    metrics = observations.PerformanceMetrics()
    monkeypatch.setattr(observations, "performance_metrics", metrics)

    async def scenario():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
            poolclass=_MeasuredQueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1,
        )
        try:
            first = await engine.connect()

            async def waiting_request():
                async with engine.connect():
                    pass

            waiting = asyncio.create_task(waiting_request())
            await asyncio.sleep(0.03)
            assert not waiting.done()
            await first.close()
            await waiting
            async with engine.connect():
                with pytest.raises(TimeoutError):
                    await engine.connect()
            summary = metrics.snapshot()["metrics"]
            assert summary["db.acquire_ms"]["count"] == 4
            assert summary["db.acquire_ms"]["max"] >= 90
            assert summary["db.acquire_ms"]["sum"] >= 120
            assert summary["db.acquire_errors"]["sum"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sql_error_commit_duration_and_hold_measurements(tmp_path, monkeypatch):
    metrics = observations.PerformanceMetrics()
    monkeypatch.setattr(observations, "performance_metrics", metrics)

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timings.db'}")
        original = engine.sync_engine.dialect.do_commit

        def slow_commit(connection):
            time.sleep(0.02)
            original(connection)

        engine.sync_engine.dialect.do_commit = slow_commit
        _instrument_engine(engine)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("CREATE TABLE probe (value INTEGER)"))
                await connection.execute(text("INSERT INTO probe VALUES (1)"))
                await connection.commit()
                with pytest.raises(OperationalError):
                    await connection.execute(text("SELECT * FROM missing_table"))
            summary = metrics.snapshot()["metrics"]
            assert summary["db.sql_ms"]["count"] == 3
            assert summary["db.sql_errors"]["sum"] == 1
            assert summary["db.commit_ms"]["count"] == 1
            assert summary["db.commit_ms"]["max"] >= 20
            assert summary["db.hold_ms"]["count"] == 1
            assert summary["db.hold_ms"]["max"] >= summary["db.commit_ms"]["max"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_suppression_excludes_own_reads_and_preserves_sqlite_memory_pool(monkeypatch):
    metrics = observations.PerformanceMetrics()
    monkeypatch.setattr(observations, "performance_metrics", metrics)

    async def scenario():
        engine = create_database_engine("sqlite+aiosqlite:///:memory:")
        assert isinstance(engine.pool, StaticPool)
        try:
            with observations.suppress_metrics():
                async with engine.begin() as connection:
                    await connection.execute(text("SELECT 1"))
            assert all(row["count"] == 0 for row in metrics.snapshot()["metrics"].values())
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            assert metrics.snapshot()["metrics"]["db.sql_ms"]["count"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
