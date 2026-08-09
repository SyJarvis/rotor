import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.control.requests import router
from rotor.core.control_auth import (
    ControlAuthConfig,
    ControlAPIException,
    control_api_exception_handler,
    get_control_auth_config,
)
from rotor.database import Base, get_db
from rotor.models.request_attempt import RequestAttempt


def _attempt(
    *,
    request_id: str,
    category: str,
    started_at: datetime,
    outcome: str = "failed",
    channel_id: int = 1,
    agent_run_id: str | None = None,
    upstream_status: int | None = 503,
) -> RequestAttempt:
    return RequestAttempt(
        request_id=request_id,
        attempt_index=0,
        channel_id=channel_id,
        requested_model="model-a",
        provider_model="provider-a",
        request_protocol="openai_chat",
        provider_protocol="openai",
        started_at=started_at,
        finished_at=started_at,
        outcome=outcome,
        upstream_status=upstream_status,
        error_category=category,
        error_code="upstream_unavailable",
        provider_request_ids={},
        request_origin=(
            "rotor_agent" if agent_run_id is not None else "client"
        ),
        agent_run_id=agent_run_id,
    )


def test_recent_failures_groups_filters_and_excludes_current_agent_run() -> None:
    async def exercise() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add_all([
                _attempt(
                    request_id="req-timeout",
                    category="timeout",
                    started_at=now,
                ),
                _attempt(
                    request_id="req-network",
                    category="network_connectivity",
                    started_at=now - timedelta(minutes=1),
                    channel_id=2,
                ),
                _attempt(
                    request_id="req-success",
                    category="timeout",
                    started_at=now,
                    outcome="success",
                ),
                _attempt(
                    request_id="req-old",
                    category="timeout",
                    started_at=now - timedelta(hours=2),
                ),
                _attempt(
                    request_id="req-agent",
                    category="timeout",
                    started_at=now,
                    agent_run_id="run-current",
                ),
            ])
            await db.commit()

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(router, prefix="/api/control/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_control_auth_config] = lambda: (
            ControlAuthConfig(
                token="read-token",
                scopes=frozenset({"request_trace:read"}),
                actor_id="admin-1",
                client_id="rotor-mcp",
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/control/v1/failures",
                headers={
                    "Authorization": "Bearer read-token",
                    "X-Agent-Run-Id": "run-current",
                },
            )
        await engine.dispose()

        assert response.status_code == 200
        groups = response.json()["data"]
        assert {item["group"]["category"] for item in groups} == {
            "timeout",
            "network_connectivity",
        }
        timeout = next(
            item
            for item in groups
            if item["group"]["category"] == "timeout"
        )
        assert timeout["count"] == 1
        assert timeout["sample_request_ids"] == ["req-timeout"]

    asyncio.run(exercise())


def test_recent_failures_cursor_is_bound_to_filters() -> None:
    async def exercise() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add_all([
                _attempt(
                    request_id="req-timeout",
                    category="timeout",
                    started_at=now,
                ),
                _attempt(
                    request_id="req-network",
                    category="network_connectivity",
                    started_at=now - timedelta(minutes=1),
                ),
            ])
            await db.commit()

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(router, prefix="/api/control/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_control_auth_config] = lambda: (
            ControlAuthConfig(
                token="read-token",
                scopes=frozenset({"request_trace:read"}),
                actor_id="admin-1",
                client_id="rotor-mcp",
            )
        )
        headers = {"Authorization": "Bearer read-token"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.get(
                "/api/control/v1/failures?limit=1",
                headers=headers,
            )
            cursor = first.json()["page"]["next_cursor"]
            second = await client.get(
                "/api/control/v1/failures",
                params={"limit": 1, "cursor": cursor},
                headers=headers,
            )
            mismatch = await client.get(
                "/api/control/v1/failures",
                params={
                    "limit": 1,
                    "cursor": cursor,
                    "model": "other-model",
                },
                headers=headers,
            )
        await engine.dispose()

        assert first.json()["page"]["has_more"] is True
        assert second.status_code == 200
        assert second.json()["page"]["has_more"] is False
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == (
            "invalid_failure_query"
        )

    asyncio.run(exercise())


def test_recent_failures_status_group_includes_statusless_samples() -> None:
    async def exercise() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(
                _attempt(
                    request_id="req-network",
                    category="network_connectivity",
                    started_at=now,
                    upstream_status=None,
                )
            )
            await db.commit()

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(router, prefix="/api/control/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_control_auth_config] = lambda: (
            ControlAuthConfig(
                token="read-token",
                scopes=frozenset({"request_trace:read"}),
                actor_id="admin-1",
                client_id="rotor-mcp",
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/control/v1/failures?group_by=status",
                headers={"Authorization": "Bearer read-token"},
            )
        await engine.dispose()

        assert response.status_code == 200
        assert response.json()["data"][0]["group"]["status"] is None
        assert response.json()["data"][0]["sample_request_ids"] == [
            "req-network"
        ]

    asyncio.run(exercise())
