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
from rotor.main import app as rotor_app
from rotor.models.usage import UsageLedger


def _usage(
    *,
    request_id: str,
    model: str,
    created_at: datetime,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float | None = None,
    currency: str = "USD",
) -> UsageLedger:
    return UsageLedger(
        request_id=request_id,
        model=model,
        request_protocol="openai_chat",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        uncached_input_tokens=prompt_tokens,
        total_cost=cost or 0.0,
        currency=currency,
        cost_status="calculated" if cost is not None else "unknown",
        created_at=created_at,
    )


def test_control_model_usage_route_is_registered() -> None:
    operation = rotor_app.openapi()["paths"][
        "/api/control/v1/usage/models"
    ]

    assert "get" in operation


def test_model_usage_aggregates_distinct_requests_and_sorts_by_tokens() -> None:
    async def exercise() -> None:
        start = datetime(2026, 7, 29, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            setup_db.add_all([
                _usage(
                    request_id="req-a",
                    model="model-a",
                    created_at=start + timedelta(hours=1),
                    prompt_tokens=10,
                    completion_tokens=5,
                    cost=0.1,
                    currency="USD",
                ),
                _usage(
                    request_id="req-a",
                    model="model-a",
                    created_at=start + timedelta(hours=1, minutes=1),
                    prompt_tokens=2,
                    completion_tokens=1,
                    cost=0.2,
                    currency="CNY",
                ),
                _usage(
                    request_id="req-b",
                    model="model-b",
                    created_at=start + timedelta(hours=2),
                    prompt_tokens=30,
                    completion_tokens=10,
                    cost=0.5,
                    currency="CNY",
                ),
                _usage(
                    request_id="req-end",
                    model="excluded-at-end",
                    created_at=end,
                    prompt_tokens=100,
                    completion_tokens=100,
                ),
            ])
            await setup_db.commit()

        app = _control_app(sessions, scopes={"usage:read"})
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/control/v1/usage/models",
                params={
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                },
                headers={
                    "Authorization": "Bearer read-token",
                    "X-Request-Id": "control-usage-1",
                },
            )
        await engine.dispose()

        assert response.status_code == 200
        payload = response.json()
        assert payload["request_id"] == "control-usage-1"
        assert [item["model"] for item in payload["data"]] == [
            "model-b",
            "model-a",
        ]
        assert payload["data"][0]["total_tokens"] == 40
        assert payload["data"][1]["total_tokens"] == 18
        assert payload["data"][1]["request_count"] == 1
        assert payload["data"][1]["ledger_count"] == 2
        assert payload["data"][1]["uncached_input_tokens"] == 12
        assert payload["data"][1]["cache_write_tokens"] == 0
        assert payload["data"][1]["usage_v2_ledger_count"] == 2
        assert payload["data"][1]["costed_ledger_count"] == 2
        assert payload["data"][1]["cost_totals_by_currency"] == {
            "CNY": 0.2,
            "USD": 0.1,
        }
        assert payload["window"]["end_exclusive"] is True

    asyncio.run(exercise())


def test_model_usage_requires_usage_scope_and_rejects_large_window() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        active_scopes = {"value": {"request_trace:read"}}
        app = _control_app(sessions, scopes=active_scopes)
        headers = {"Authorization": "Bearer read-token"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            forbidden = await client.get(
                "/api/control/v1/usage/models",
                headers=headers,
            )
            active_scopes["value"] = {"usage:read"}
            invalid = await client.get(
                "/api/control/v1/usage/models",
                params={
                    "start_time": "2026-07-01T00:00:00Z",
                    "end_time": "2026-07-09T00:00:00Z",
                },
                headers=headers,
            )
        await engine.dispose()

        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "control_scope_required"
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == (
            "invalid_model_usage_query"
        )

    asyncio.run(exercise())


def _control_app(
    sessions,
    *,
    scopes: set[str] | dict[str, set[str]],
) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        ControlAPIException,
        control_api_exception_handler,
    )
    app.include_router(router, prefix="/api/control/v1")

    async def override_db():
        async with sessions() as db:
            yield db

    def active_scopes() -> set[str]:
        if isinstance(scopes, dict):
            return scopes["value"]
        return scopes

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_control_auth_config] = lambda: (
        ControlAuthConfig(
            token="read-token",
            scopes=frozenset(active_scopes()),
            actor_id="admin-1",
            client_id="rotor-mcp",
        )
    )
    return app
