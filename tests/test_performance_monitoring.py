import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

import rotor.observability as observations
from rotor.core.admin_auth import (
    ADMIN_CSRF_COOKIE, ADMIN_SESSION_COOKIE, create_admin_session,
    ensure_default_admin,
)
from rotor.database import Base, create_database_engine, get_db
from rotor.main import app as rotor_app


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
    return engine, sessions


@pytest.mark.parametrize("role,expected_status", [("admin", 200), ("user", 403)])
def test_performance_api_requires_admin_and_snapshot_needs_no_business_query(tmp_path, monkeypatch, role, expected_status):
    async def scenario():
        engine, sessions = await _database(tmp_path)

        async def db_override():
            async with sessions() as db:
                yield db

        monkeypatch.setitem(rotor_app.dependency_overrides, get_db, db_override)
        try:
            async with sessions() as db:
                user = await ensure_default_admin(db)
                user.must_change_password = False
                user.role = role
                session, secret, csrf = await create_admin_session(db, admin_user=user)
                await db.commit()
            async with AsyncClient(transport=ASGITransport(rotor_app), base_url="http://test") as client:
                assert (await client.get("/api/admin/monitoring/performance")).status_code == 401
                client.cookies.set(ADMIN_SESSION_COOKIE, secret)
                client.cookies.set(ADMIN_CSRF_COOKIE, csrf)
                statements = []
                event.listen(engine.sync_engine, "before_cursor_execute",
                             lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
                response = await client.get("/api/admin/monitoring/performance")
                assert response.status_code == expected_status
                if role == "admin":
                    assert "store" in response.json() and "metrics" in response.json()
                assert statements and all("admin_sessions" in statement for statement in statements)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("authenticated", [False, True])
def test_retired_reconciliation_endpoints_are_absent_in_the_application(tmp_path, monkeypatch, authenticated):
    async def scenario():
        engine, sessions = await _database(tmp_path)

        async def db_override():
            async with sessions() as db:
                yield db

        monkeypatch.setitem(rotor_app.dependency_overrides, get_db, db_override)
        try:
            async with sessions() as db:
                admin = await ensure_default_admin(db)
                admin.must_change_password = False
                _, secret, csrf = await create_admin_session(db, admin_user=admin)
                await db.commit()
            async with AsyncClient(transport=ASGITransport(rotor_app), base_url="http://test") as client:
                if authenticated:
                    client.cookies.set(ADMIN_SESSION_COOKIE, secret)
                    client.cookies.set(ADMIN_CSRF_COOKIE, csrf)
                    assert (await client.get("/api/admin/monitoring/performance")).status_code == 200
                schema = await client.get("/openapi.json")
                assert schema.status_code == 200
                paths = ("/api/admin/monitoring/reconciliation", "/api/admin/monitoring/reconciliation/reset")
                assert all(path not in schema.json()["paths"] for path in paths)
                statements = []
                event.listen(engine.sync_engine, "before_cursor_execute",
                             lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
                for path in paths:
                    for method, expected in (("GET", 404), ("POST", 405)):
                        response = await client.request(method, path,
                            headers={"X-CSRF-Token": csrf} if authenticated else {})
                        assert response.status_code == expected
                assert statements == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
