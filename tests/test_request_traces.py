import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.database import Base
from rotor.models.conversation import ConversationRecord
from rotor.models.request_attempt import RequestAttempt
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.usage import UsageLedger
from rotor.services.request_traces import get_request_trace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_request_trace_aggregates_complete_request_facts() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions() as db:
            db.add(
                RoutingDecisionRecord(
                    request_id="req-1",
                    token_id=1,
                    model="model-a",
                    request_protocol="openai_chat",
                    strategy="fallback_order",
                    policy_version="fallback_order-v1",
                    candidate_channel_ids=[1, 2],
                    selected_channel_id=1,
                    required_capabilities=[],
                    affinity_used=False,
                    score_snapshot=None,
                    feature_snapshot={"stream": False},
                )
            )
            db.add_all(
                [
                    RequestAttempt(
                        request_id="req-1",
                        attempt_index=0,
                        channel_id=1,
                        requested_model="model-a",
                        provider_model="provider-a",
                        request_protocol="openai_chat",
                        provider_protocol="openai",
                        started_at=_now(),
                        finished_at=_now(),
                        latency_ms=20,
                        outcome="failed",
                        upstream_status=503,
                        error_category="upstream_availability",
                        error_code="upstream_unavailable",
                        retryable=True,
                        retry_same_channel=True,
                        fallback_allowed=True,
                        sanitized_error={
                            "schema_version": "1",
                            "code": "upstream_unavailable",
                            "category": "upstream_availability",
                            "phase": "provider_request",
                            "upstream_status": 503,
                            "retryable": True,
                            "retry_same_channel": True,
                            "fallback_allowed": True,
                            "retry_after_seconds": None,
                            "message": "Upstream returned HTTP 503",
                            "provider_request_ids": {},
                            "occurred_at": _now().isoformat(),
                        },
                        provider_request_ids={},
                        request_origin="rotor_agent",
                        agent_run_id="run-1",
                    ),
                    RequestAttempt(
                        request_id="req-1",
                        attempt_index=1,
                        channel_id=2,
                        requested_model="model-a",
                        provider_model="provider-a",
                        request_protocol="openai_chat",
                        provider_protocol="openai",
                        started_at=_now(),
                        finished_at=_now(),
                        latency_ms=30,
                        outcome="success",
                        provider_request_ids={},
                        request_origin="rotor_agent",
                        agent_run_id="run-1",
                    ),
                ]
            )
            db.add_all(
                [
                    UsageLedger(
                        request_id="req-1",
                        conversation_id="conv-1",
                        token_id=1,
                        channel_id=1,
                        model="model-a",
                        request_protocol="openai_chat",
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        usage_source="missing",
                        status="failed",
                    ),
                    UsageLedger(
                        request_id="req-1",
                        conversation_id="conv-1",
                        token_id=1,
                        channel_id=2,
                        model="model-a",
                        request_protocol="openai_chat",
                        prompt_tokens=10,
                        completion_tokens=5,
                        total_tokens=15,
                        cached_tokens=2,
                        usage_source="provider",
                        status="success",
                    ),
                ]
            )
            db.add(
                ConversationRecord(
                    conversation_id="conv-1",
                    request_id="req-1",
                    token_id=1,
                    channel_id=2,
                    model="model-a",
                    protocol="openai_chat",
                    provider="openai",
                    file_path="/tmp/conversation.jsonl",
                    status="success",
                )
            )
            await db.commit()

            trace = await get_request_trace(db, "req-1")
        await engine.dispose()

        assert trace is not None
        assert trace.origin == "rotor_agent"
        assert trace.agent_run_id == "run-1"
        assert trace.requested_model == "model-a"
        assert trace.routing_decision.candidate_channel_ids == [1, 2]
        assert [attempt.attempt_index for attempt in trace.attempts] == [0, 1]
        assert trace.attempts[0].error.code == "upstream_unavailable"
        assert trace.attempts[0].error.fallback_allowed is True
        assert trace.usage.total_tokens == 15
        assert trace.usage.cached_tokens == 2
        assert trace.final_outcome == "success"
        assert trace.meta.warnings == []

    asyncio.run(exercise())


def test_request_trace_reports_missing_facts_without_guessing() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions() as db:
            db.add(
                RequestAttempt(
                    request_id="req-partial",
                    attempt_index=0,
                    channel_id=1,
                    requested_model="model-a",
                    provider_model="provider-a",
                    request_protocol="openai_chat",
                    provider_protocol="openai",
                    started_at=_now(),
                    finished_at=_now(),
                    latency_ms=20,
                    outcome="failed",
                    provider_request_ids={},
                    request_origin="client",
                )
            )
            await db.commit()
            trace = await get_request_trace(db, "req-partial")
        await engine.dispose()

        assert trace is not None
        assert trace.routing_decision is None
        assert trace.usage is None
        assert trace.final_outcome == "failed"
        assert trace.meta.warnings == [
            "missing_routing_decision",
            "missing_usage",
            "missing_conversation_record",
            "attempt_0_missing_error_fact",
        ]

    asyncio.run(exercise())


def test_request_trace_returns_none_for_unknown_request() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            trace = await get_request_trace(db, "req-missing")
        await engine.dispose()
        assert trace is None

    asyncio.run(exercise())
