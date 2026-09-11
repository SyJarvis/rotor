import asyncio
import os
from types import SimpleNamespace

import pytest

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import rotor.observability as observations
from rotor.api.admin.monitoring import router
from rotor.core.admin_auth import (
    ADMIN_CSRF_COOKIE, ADMIN_SESSION_COOKIE, create_admin_session,
    ensure_default_admin, require_admin_access,
)
from rotor.database import Base, create_database_engine, get_db
from rotor.models.channel import Channel  # noqa: F401
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


def test_collector_bounds_samples_and_keeps_exact_lifetime_counts(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(observations.time, "monotonic", lambda: now)
    metrics = observations.PerformanceMetrics(sample_limit=3)
    for value in range(1, 6):
        metrics.observe("db.sql_ms", value)
    metrics.observe("unbounded-secret-name", 1)
    metrics.observe("db.sql_ms", float("nan"))
    metrics.request("chat", True, False, True, 12, 3)
    metrics.request("unknown-client", False, True, True, 15, None)
    snapshot = metrics.snapshot()
    sql = snapshot["metrics"]["db.sql_ms"]
    assert (sql["count"], sql["sum"], sql["sample_count"], sql["p50"], sql["p95"]) == (5, 15, 3, 4, 5)
    assert snapshot["requests"]["chat"]["success"] == 1
    assert snapshot["requests"]["other"]["cancelled"] == 1
    assert snapshot["requests"]["other"]["ttft_ms"]["p50"] is None
    assert "unbounded-secret-name" not in snapshot["metrics"]
    now += 301
    sql = metrics.snapshot()["metrics"]["db.sql_ms"]
    assert (sql["count"], sql["sum"], sql["sample_count"], sql["p50"]) == (5, 15, 0, None)


async def _database(tmp_path):
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitoring.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(Token(id=1, key="monitor-test", name="test", request_count=5, token_count=50, used_quota=50))
        db.add(UsageLedger(id=1000, request_id="history", token_id=1, model="test",
                           request_protocol="openai_chat", total_tokens=50, status="success"))
        await db.commit()
    return engine, sessions


async def _usage(sessions, request_id, *, counter_delta=10):
    async with sessions() as db:
        await db.execute(update(Token).where(Token.id == 1).values(
            request_count=Token.request_count + 1,
            token_count=Token.token_count + counter_delta,
            used_quota=Token.used_quota + counter_delta,
            enabled=False,
        ))
        db.add(UsageLedger(request_id=request_id, token_id=1, model="test",
                           request_protocol="openai_chat", total_tokens=10, status="success"))
        await db.commit()


def test_reconciliation_incremental_cache_reset_invalid_and_no_writes(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _database(tmp_path)
        monitor = observations.ReconciliationMonitor(sessions, cache_seconds=0)
        metrics = observations.PerformanceMetrics()
        monkeypatch.setattr(observations, "performance_metrics", metrics)
        statements = []
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
        try:
            baseline = await monitor.check()
            assert baseline["status"] == "baseline"
            assert baseline["pid"] == os.getpid()
            assert not any("sum(" in statement.lower() for statement in statements)
            assert all(row["count"] == 0 for row in metrics.snapshot()["metrics"].values())
            assert not any(statement.upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
            await _usage(sessions, "first")
            statements.clear()
            result = await monitor.check()
            assert result["status"] == "ok"  # Automatic enabled=False is not invalidation.
            assert result["requests"] == {"ledger": 1, "token": 1}
            assert result["total_tokens"] == {"ledger": 10, "token": 10, "quota": 10}
            assert any("usage_ledger.id >" in statement for statement in statements)
            assert not any(statement.upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
            monitor.cache_seconds = 30
            statements.clear()
            assert (await monitor.check())["cached"] is True
            assert statements == []
            monitor.cache_seconds = 0
            await _usage(sessions, "lost-count", counter_delta=0)
            assert (await monitor.check())["status"] == "mismatch"
            async with sessions() as db:
                await db.execute(update(Token).values(used_quota=0))
                await db.commit()
            assert (await monitor.check())["status"] == "invalid"
            statements.clear()
            reset = await monitor.reset()
            assert reset["since"] is None and reset["checked_at"] is None
            assert statements == []
            assert (await monitor.check())["status"] == "baseline"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_reconciliation_uses_one_sqlite_snapshot_during_concurrent_commit(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _database(tmp_path)
        monitor = observations.ReconciliationMonitor(sessions, cache_seconds=0)
        try:
            await monitor.check()
            original = AsyncSession.execute
            injected = False

            async def execute(db, statement, *args, **kwargs):
                nonlocal injected
                result = await original(db, statement, *args, **kwargs)
                if not injected and str(statement).startswith("SELECT tokens.id"):
                    injected = True
                    await _usage(sessions, "concurrent")
                return result

            monkeypatch.setattr(AsyncSession, "execute", execute)
            before = await monitor.check()
            assert before["status"] == "ok"
            assert before["requests"] == {"ledger": 0, "token": 0}
            after = await monitor.check()
            assert after["status"] == "ok"
            assert after["requests"] == {"ledger": 1, "token": 1}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_performance_api_auth_csrf_and_snapshot_needs_no_business_query(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _database(tmp_path)
        monitor = observations.ReconciliationMonitor(sessions)
        monkeypatch.setattr(observations, "reconciliation_monitor", monitor)
        app = FastAPI()
        app.include_router(router, prefix="/api/admin", dependencies=[Depends(require_admin_access)])

        async def db_override():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = db_override
        try:
            async with sessions() as db:
                admin = await ensure_default_admin(db)
                admin.must_change_password = False
                session, secret, csrf = await create_admin_session(db, admin_user=admin)
                await db.commit()
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                assert (await client.get("/api/admin/monitoring/performance")).status_code == 401
                client.cookies.set(ADMIN_SESSION_COOKIE, secret)
                client.cookies.set(ADMIN_CSRF_COOKIE, csrf)
                statements = []
                event.listen(engine.sync_engine, "before_cursor_execute",
                             lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
                response = await client.get("/api/admin/monitoring/performance")
                assert response.status_code == 200
                assert "store" in response.json() and "metrics" in response.json()
                assert all("admin_sessions" in statement for statement in statements)
                assert (await client.post("/api/admin/monitoring/reconciliation/reset")).status_code == 403
                response = await client.post("/api/admin/monitoring/reconciliation/reset", headers={"X-CSRF-Token": csrf})
                assert response.status_code == 200
                assert response.json()["since"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["quota", "new-token", "deleted-token"])
def test_token_configuration_changes_invalidate_baseline(tmp_path, mutation):
    async def scenario():
        engine, sessions = await _database(tmp_path)
        monitor = observations.ReconciliationMonitor(sessions, cache_seconds=0)
        try:
            await monitor.check()
            async with sessions() as db:
                if mutation == "quota":
                    await db.execute(update(Token).values(quota=123))
                elif mutation == "new-token":
                    db.add(Token(key="new", name="new"))
                else:
                    await db.delete(await db.get(Token, 1))
                await db.commit()
            assert (await monitor.check())["status"] == "invalid"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_checks_share_cached_snapshot_and_recover_from_error(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _database(tmp_path)
        monitor = observations.ReconciliationMonitor(sessions)
        original = monitor._read_snapshot
        calls = 0

        async def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary database failure")
            return await original()

        monkeypatch.setattr(monitor, "_read_snapshot", read)
        try:
            error = await monitor.check()
            assert error["status"] == "error"
            monitor._checked_monotonic = 0
            results = await asyncio.gather(*(monitor.check() for _ in range(5)))
            assert calls == 2
            assert all(result["status"] == "baseline" for result in results)
            assert results[0]["since"] == results[0]["checked_at"]
            assert results[0]["since"] != error["since"]
            assert sum(not result["cached"] for result in results) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgresql_reconciliation_is_explicitly_unsupported():
    class Session:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    result = asyncio.run(observations.ReconciliationMonitor(Session).check())
    assert result["status"] == "error"
    assert "仅支持 SQLite" in result["reason"]
