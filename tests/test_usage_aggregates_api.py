import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin import logs
from rotor.database import Base, get_db
from rotor.models.channel import Channel
from rotor.models.log import RequestLog


START = datetime(2026, 8, 10, 16)
END = START + timedelta(days=1)


def request_log(created_at, model, channel_id, tokens, *, success=True, cost=None, currency="USD", cost_status=None):
    return RequestLog(
        token_id=1, channel_id=channel_id, model=model, request_model=model,
        prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens,
        uncached_input_tokens=tokens, success=success, created_at=created_at,
        cost=cost, currency=currency,
        cost_status=cost_status or ("calculated" if cost is not None else "unknown"),
    )


def sample_logs():
    return [
        request_log(START - timedelta(seconds=1), "alpha", 1, 1000, cost=100),
        request_log(START, "alpha", 1, 10, cost=1),
        request_log(START + timedelta(minutes=20), "alpha", 1, 20, success=False, cost=999, cost_status="unknown"),
        request_log(START + timedelta(minutes=40), "alpha", 2, 30, cost=0),
        request_log(START + timedelta(hours=1), "alpha", 2, 40, success=False, cost=7, currency="CNY"),
        request_log(START + timedelta(hours=2), "beta", 99, 50, cost=2),
        request_log(START + timedelta(hours=3), "zero", 2, 7, cost=0),
        request_log(END - timedelta(seconds=1), "beta", None, 60, success=False),
        request_log(END, "alpha", 1, 2000, cost=200),
    ]


@asynccontextmanager
async def usage_api(tmp_path, monkeypatch, entries=None, timezone_name="Asia/Shanghai"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(logs.settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(logs.application_settings, "get", lambda: SimpleNamespace(display_timezone=timezone_name))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            Channel(id=1, name="Primary", type="openai", key="test", base_url="https://example.test"),
            Channel(id=2, name="Secondary", type="openai", key="test", base_url="https://example.test"),
            *(sample_logs() if entries is None else entries),
        ])
        await db.commit()
    app = FastAPI()

    async def db_dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.include_router(logs.router, prefix="/api/admin")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bucket", ["hour", "day"])
@pytest.mark.parametrize("window", [
    {"start_date": "2026-08-11", "end_date": "2026-08-11"},
    {"period": "day", "period_date": "2026-08-11"},
    {"start_time": "2026-08-11T00:00:00+08:00", "end_time": "2026-08-12T00:00:00+08:00"},
])
@pytest.mark.parametrize("filters,requests,tokens,failed", [
    ({}, 7, 217, 3), ({"model": "alpha"}, 4, 100, 2),
    ({"channel_id": 1}, 2, 30, 1), ({"model": "alpha", "channel_id": 2}, 2, 70, 1),
    ({"channel_id": 99}, 1, 50, 0), ({"model": "missing"}, 0, 0, 0),
])
def test_channel_and_model_timelines_match_stats_windows_and_filters(tmp_path, monkeypatch, bucket, window, filters, requests, tokens, failed):
    async def exercise():
        async with usage_api(tmp_path, monkeypatch) as (client, _):
            params = {**window, **filters}
            stats = await client.get("/api/admin/logs/stats", params=params)
            channels = await client.get("/api/admin/logs/channels", params=params)
            timeline = await client.get("/api/admin/logs/timeseries_by_model", params={**params, "bucket": bucket})
            assert stats.status_code == channels.status_code == timeline.status_code == 200
            stats, channels, timeline = stats.json(), channels.json(), timeline.json()
            assert stats["total_requests"] == requests == sum(row["total_requests"] for row in channels)
            assert stats["failed_requests"] == failed == sum(row["failed_requests"] for row in channels)
            assert stats["success_requests"] == requests - failed == sum(row["success_requests"] for row in channels)
            assert sum(row["requests"] for row in timeline) == requests
            assert sum(row["tokens"] for row in timeline) == stats["total_tokens"] == tokens
            assert sum(row["costed_requests"] for row in timeline) == stats["costed_requests"]
            costs = {}
            for row in timeline:
                for currency, value in row["cost_totals_by_currency"].items():
                    costs[currency] = costs.get(currency, 0) + value
            assert costs == stats["cost_totals_by_currency"]
            if not requests:
                assert channels == timeline == []

    asyncio.run(exercise())


def test_channel_rows_include_deleted_and_unknown_channels_without_lifetime_counters(tmp_path, monkeypatch):
    async def exercise():
        async with usage_api(tmp_path, monkeypatch) as (client, engine):
            queries = []
            event.listen(engine.sync_engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: queries.append(statement))
            response = await client.get("/api/admin/logs/channels", params={"start_date": "2026-08-11", "end_date": "2026-08-11"})
            assert response.status_code == 200
            rows = response.json()
            assert len(queries) == 1
            assert rows[:2] == [
                {"channel_id": 2, "channel_name": "Secondary", "total_requests": 3, "success_requests": 2, "failed_requests": 1},
                {"channel_id": 1, "channel_name": "Primary", "total_requests": 2, "success_requests": 1, "failed_requests": 1},
            ]
            assert {row["channel_id"]: row for row in rows[2:]} == {
                None: {"channel_id": None, "channel_name": "Unknown channel", "total_requests": 1, "success_requests": 0, "failed_requests": 1},
                99: {"channel_id": 99, "channel_name": "Channel #99", "total_requests": 1, "success_requests": 1, "failed_requests": 0},
            }
            assert (await client.get("/api/admin/logs/channels", params={"start_date": "2026-08-11", "end_date": "2026-08-11", "token_id": 2})).json() == []

    asyncio.run(exercise())


def test_timeline_costs_preserve_unknown_zero_and_multiple_currencies_without_n_plus_one(tmp_path, monkeypatch):
    async def exercise():
        async with usage_api(tmp_path, monkeypatch) as (client, engine):
            queries = []
            event.listen(engine.sync_engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: queries.append(statement))
            response = await client.get("/api/admin/logs/timeseries_by_model", params={"start_date": "2026-08-11", "end_date": "2026-08-11", "bucket": "day"})
            assert response.status_code == 200 and len(queries) == 2
            rows = {(row["bucket"], row["model"]): row for row in response.json()}
            assert rows[("2026-08-10", "alpha")] == {
                "bucket": "2026-08-10", "model": "alpha", "tokens": 100, "requests": 4,
                "total_cost": None, "currency": None, "cost_totals_by_currency": {"USD": 1.0, "CNY": 7.0}, "costed_requests": 3,
            }
            zero = rows[("2026-08-10", "zero")]
            assert zero["total_cost"] == 0.0 and zero["currency"] == "USD"
            assert zero["cost_totals_by_currency"] == {"USD": 0.0} and zero["costed_requests"] == 1
            unknown = rows[("2026-08-11", "beta")]
            assert unknown["total_cost"] is None and unknown["currency"] is None
            assert unknown["cost_totals_by_currency"] == {} and unknown["costed_requests"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("endpoint", ["channels", "timeseries_by_model"])
@pytest.mark.parametrize("params", [
    {"start_date": "2026-08-11"},
    {"start_date": "2026-08-12", "end_date": "2026-08-11"},
    {"period": "day", "start_time": "2026-08-11T00:00:00Z"},
    {"start_time": "2026-08-11T00:00:00Z", "end_time": "2026-08-11T00:00:00Z"},
])
def test_usage_aggregates_reject_the_same_invalid_windows_as_stats(tmp_path, monkeypatch, endpoint, params):
    async def exercise():
        async with usage_api(tmp_path, monkeypatch, entries=[]) as (client, _):
            stats = await client.get("/api/admin/logs/stats", params=params)
            response = await client.get("/api/admin/logs/" + endpoint, params=params)
            assert response.status_code == stats.status_code == 422
            assert response.json() == stats.json()

    asyncio.run(exercise())


def test_day_window_with_dst_transition_keeps_stats_and_aggregates_consistent(tmp_path, monkeypatch):
    async def exercise():
        start = datetime(2025, 11, 2, 4)
        end = start + timedelta(hours=25)
        entries = [request_log(at, "dst", 1, tokens, cost=tokens) for at, tokens in [
            (start - timedelta(seconds=1), 1000), (start, 10),
            (end - timedelta(seconds=1), 20), (end, 2000),
        ]]
        async with usage_api(tmp_path, monkeypatch, entries=entries, timezone_name="America/New_York") as (client, _):
            params = {"period": "day", "period_date": "2025-11-02"}
            stats = (await client.get("/api/admin/logs/stats", params=params)).json()
            channels = (await client.get("/api/admin/logs/channels", params=params)).json()
            for bucket in ("hour", "day"):
                timeline = (await client.get("/api/admin/logs/timeseries_by_model", params={**params, "bucket": bucket})).json()
                assert sum(row["requests"] for row in timeline) == stats["total_requests"] == channels[0]["total_requests"] == 2
                assert sum(row["tokens"] for row in timeline) == stats["total_tokens"] == 30
                assert sum(row["total_cost"] for row in timeline) == stats["total_cost"] == 30

    asyncio.run(exercise())


def test_empty_usage_and_rolling_days_keep_the_stats_selection(tmp_path, monkeypatch):
    async def exercise():
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        entries = [request_log(now - timedelta(hours=1), "recent", 1, 10), request_log(now - timedelta(days=2), "old", 2, 100)]
        async with usage_api(tmp_path, monkeypatch, entries=entries) as (client, _):
            for params in ({"days": 1}, {"days": 1, "model": "missing"}):
                stats = (await client.get("/api/admin/logs/stats", params=params)).json()
                channels = (await client.get("/api/admin/logs/channels", params=params)).json()
                timeline = (await client.get("/api/admin/logs/timeseries_by_model", params=params)).json()
                assert sum(row["requests"] for row in timeline) == stats["total_requests"] == sum(row["total_requests"] for row in channels)
                assert sum(row["tokens"] for row in timeline) == stats["total_tokens"]
                if "model" in params:
                    assert channels == timeline == []
                else:
                    assert stats["total_requests"] == 1 and timeline[0]["total_cost"] is None

    asyncio.run(exercise())
