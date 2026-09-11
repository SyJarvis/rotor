import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.control.requests import router
from rotor.core.control_auth import (
    ControlAPIException,
    ControlAuthConfig,
    control_api_exception_handler,
    get_control_auth_config,
)
from rotor.database import Base, get_db
from rotor.main import app as rotor_app
from rotor.models.request_attempt import RequestAttempt
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLeaseEvent
from rotor.models.usage import UsageLedger


def _routing(
    *,
    request_id: str,
    model: str,
    created_at: datetime,
    session_source: str | None,
    preferred_channel_id: int | None,
    lease_used: bool,
) -> RoutingDecisionRecord:
    return RoutingDecisionRecord(
        request_id=request_id,
        token_id=1,
        model=model,
        request_protocol="openai_chat",
        strategy="priority_weighted",
        candidate_channel_ids=[1, 2],
        selected_channel_id=1,
        required_capabilities=[],
        affinity_used=session_source is not None,
        feature_snapshot={
            "session_source": session_source,
            "session_lease": {
                "preferred_channel_id": preferred_channel_id,
                "used": lease_used,
            },
        },
        created_at=created_at,
    )


def _lease_event(
    *,
    event_id: int,
    request_id: str,
    event_type: str,
    reason: str,
    created_at: datetime,
) -> SessionLeaseEvent:
    return SessionLeaseEvent(
        lease_id=1,
        request_id=request_id,
        token_id=1,
        session_id="session-a",
        logical_model="model-a",
        event_type=event_type,
        previous_channel_id=1 if event_type != "assigned" else None,
        channel_id=None if event_type == "expired" else 1,
        reason=reason,
        created_at=created_at + timedelta(seconds=event_id),
    )


def _usage(
    *,
    request_id: str,
    model: str,
    created_at: datetime,
    prompt_tokens: int,
    uncached_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    cost: float,
    currency: str,
    channel_id: int = 1,
    tariff_period: str = "off_peak",
) -> UsageLedger:
    return UsageLedger(
        request_id=request_id,
        model=model,
        request_protocol="openai_chat",
        channel_id=channel_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=10,
        total_tokens=prompt_tokens + 10,
        uncached_input_tokens=uncached_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        usage_source="provider",
        usage_schema_version="2",
        total_cost=cost,
        input_cost=cost,
        output_cost=0.0,
        currency=currency,
        cost_status="calculated",
        tariff_version="tariff-v1",
        tariff_period=tariff_period,
        status="success",
        created_at=created_at,
    )


def _attempt(
    *,
    request_id: str,
    attempt_index: int,
    outcome: str,
    started_at: datetime,
    status: int | None = None,
) -> RequestAttempt:
    return RequestAttempt(
        request_id=request_id,
        attempt_index=attempt_index,
        channel_id=attempt_index + 1,
        requested_model="model-a",
        provider_model="provider-model-a",
        request_protocol="openai_chat",
        provider_protocol="openai",
        started_at=started_at + timedelta(seconds=attempt_index),
        finished_at=started_at + timedelta(seconds=attempt_index + 1),
        outcome=outcome,
        upstream_status=status,
        provider_request_ids={},
        request_origin="client",
    )


def test_control_session_lease_evaluation_route_is_registered() -> None:
    operation = rotor_app.openapi()["paths"][
        "/api/control/v1/usage/session-leases"
    ]

    assert "get" in operation


def test_session_lease_evaluation_aggregates_model_facts() -> None:
    async def exercise() -> None:
        start = datetime(2026, 8, 19, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            setup_db.add_all([
                _routing(
                    request_id="req-1",
                    model="model-a",
                    created_at=start + timedelta(hours=1),
                    session_source="codex-session-id",
                    preferred_channel_id=1,
                    lease_used=True,
                ),
                _routing(
                    request_id="req-2",
                    model="model-a",
                    created_at=start + timedelta(hours=2),
                    session_source="codex-session-id",
                    preferred_channel_id=1,
                    lease_used=False,
                ),
                _routing(
                    request_id="req-3",
                    model="model-a",
                    created_at=start + timedelta(hours=3),
                    session_source=None,
                    preferred_channel_id=None,
                    lease_used=False,
                ),
                _routing(
                    request_id="req-other",
                    model="model-b",
                    created_at=start + timedelta(hours=4),
                    session_source=None,
                    preferred_channel_id=None,
                    lease_used=False,
                ),
            ])
            setup_db.add_all([
                _lease_event(
                    event_id=1,
                    request_id="req-1",
                    event_type="assigned",
                    reason="first_success",
                    created_at=start + timedelta(hours=1),
                ),
                _lease_event(
                    event_id=2,
                    request_id="req-1b",
                    event_type="renewed",
                    reason="lease_hit",
                    created_at=start + timedelta(hours=1),
                ),
                _lease_event(
                    event_id=3,
                    request_id="req-2",
                    event_type="migrated",
                    reason="fallback_success",
                    created_at=start + timedelta(hours=2),
                ),
                _lease_event(
                    event_id=4,
                    request_id="req-2",
                    event_type="expired",
                    reason="idle_timeout",
                    created_at=start + timedelta(hours=2),
                ),
                _lease_event(
                    event_id=5,
                    request_id="req-reassessment",
                    event_type="renewed",
                    reason="protocol_reassessment_deferred",
                    created_at=start + timedelta(hours=2),
                ),
            ])
            setup_db.add_all([
                _usage(
                    request_id="req-1",
                    model="model-a",
                    created_at=start + timedelta(hours=1),
                    prompt_tokens=100,
                    uncached_tokens=40,
                    cached_tokens=50,
                    cache_write_tokens=10,
                    cost=0.1,
                    currency="USD",
                ),
                _usage(
                    request_id="req-2",
                    model="model-a",
                    created_at=start + timedelta(hours=2),
                    prompt_tokens=100,
                    uncached_tokens=60,
                    cached_tokens=20,
                    cache_write_tokens=20,
                    cost=0.2,
                    currency="USD",
                    channel_id=2,
                    tariff_period="peak",
                ),
                _usage(
                    request_id="req-other",
                    model="model-b",
                    created_at=start + timedelta(hours=4),
                    prompt_tokens=50,
                    uncached_tokens=50,
                    cached_tokens=0,
                    cache_write_tokens=0,
                    cost=0.3,
                    currency="CNY",
                ),
            ])
            setup_db.add_all([
                _attempt(
                    request_id="req-1",
                    attempt_index=0,
                    outcome="success",
                    started_at=start + timedelta(hours=1),
                ),
                _attempt(
                    request_id="req-2",
                    attempt_index=0,
                    outcome="failed",
                    status=503,
                    started_at=start + timedelta(hours=2),
                ),
                _attempt(
                    request_id="req-2",
                    attempt_index=1,
                    outcome="success",
                    started_at=start + timedelta(hours=2),
                ),
                _attempt(
                    request_id="req-3",
                    attempt_index=0,
                    outcome="failed",
                    status=429,
                    started_at=start + timedelta(hours=3),
                ),
            ])
            await setup_db.commit()

        app = _control_app(sessions, scopes={"usage:read"})
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/control/v1/usage/session-leases",
                params={
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "model": "model-a",
                },
                headers={
                    "Authorization": "Bearer read-token",
                    "X-Request-Id": "control-lease-1",
                },
            )
            all_models = await client.get(
                "/api/control/v1/usage/session-leases",
                params={
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                },
                headers={"Authorization": "Bearer read-token"},
            )
        await engine.dispose()

        assert response.status_code == 200
        payload = response.json()
        data = payload["data"]
        assert payload["request_id"] == "control-lease-1"
        assert data["model"] == "model-a"
        assert data["routing_decision_count"] == 3
        assert data["stable_session_decision_count"] == 2
        assert data["stable_session_coverage_rate"] == 2 / 3
        assert data["lease_preferred_decision_count"] == 2
        assert data["lease_applied_decision_count"] == 1
        assert data["lease_application_rate"] == 0.5
        assert data["lease_event_counts"] == {
            "assigned": 1,
            "renewed": 1,
            "migrated": 1,
            "expired": 1,
        }
        assert data["continuation_count"] == 2
        assert data["migration_rate"] == 0.5
        assert data["fallback_migration_count"] == 1
        assert data["success_usage_ledger_count"] == 2
        assert data["provider_usage_coverage_rate"] == 1.0
        assert data["usage_v2_coverage_rate"] == 1.0
        assert data["cost_coverage_rate"] == 1.0
        assert data["prompt_tokens"] == 200
        assert data["uncached_input_rate"] == 0.5
        assert data["cache_read_rate"] == 0.35
        assert data["cache_write_rate"] == 0.15
        assert data["cost_totals_by_currency"] == {
            "USD": pytest.approx(0.3)
        }
        assert [
            (cohort["channel_id"], cohort["tariff_period"])
            for cohort in data["costed_cohorts"]
        ] == [(1, "off_peak"), (2, "peak")]
        assert data["costed_cohorts"][0]["cache_read_rate"] == 0.5
        assert data["costed_cohorts"][1]["total_cost"] == pytest.approx(0.2)
        assert data["attempt_request_count"] == 3
        assert data["fallback_request_count"] == 1
        assert data["fallback_success_count"] == 1
        assert data["fallback_success_rate"] == 1.0
        assert data["rate_limited_attempt_count"] == 1
        assert data["server_error_attempt_count"] == 1
        assert data["facts_complete_for_evaluation"] is True
        assert data["blocking_reasons"] == []
        assert all_models.json()["data"]["facts_complete_for_evaluation"] is False
        assert "multiple_currencies" in all_models.json()["data"][
            "blocking_reasons"
        ]

    asyncio.run(exercise())


def test_session_lease_evaluation_reports_empty_and_invalid_windows() -> None:
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
                "/api/control/v1/usage/session-leases",
                headers=headers,
            )
            active_scopes["value"] = {"usage:read"}
            empty = await client.get(
                "/api/control/v1/usage/session-leases",
                params={
                    "start_time": "2026-08-01T00:00:00Z",
                    "end_time": "2026-08-02T00:00:00Z",
                },
                headers=headers,
            )
            invalid = await client.get(
                "/api/control/v1/usage/session-leases",
                params={
                    "start_time": "2026-07-01T00:00:00Z",
                    "end_time": "2026-08-01T00:00:01Z",
                },
                headers=headers,
            )
        await engine.dispose()

        assert forbidden.status_code == 403
        assert empty.status_code == 200
        assert empty.json()["data"]["blocking_reasons"] == [
            "no_routing_decisions",
            "no_stable_session_decisions",
            "no_session_lease_events",
            "no_success_usage",
        ]
        assert empty.json()["data"]["stable_session_coverage_rate"] is None
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == (
            "invalid_session_lease_evaluation_query"
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
