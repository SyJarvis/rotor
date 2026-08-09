import asyncio
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.core.exceptions import normalize_upstream_error
from rotor.gateway.attempts import AttemptContext, AttemptRecorder
from rotor.models.request_attempt import RequestAttempt


def _channel():
    return SimpleNamespace(id=12, protocol="openai")


class RecordingSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.added = []
        self.commits = 0
        self.fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")


class RecordingSessionFactory:
    def __init__(self, session: RecordingSession) -> None:
        self.session = session
        self.calls = 0

    def __call__(self) -> RecordingSession:
        self.calls += 1
        return self.session


def test_record_attempt_commits_success_context_independently() -> None:
    session = RecordingSession()
    context = AttemptContext.start(
        1,
        request_origin="rotor_agent",
        agent_run_id="run-1",
    )

    recorded = asyncio.run(
        AttemptRecorder(RecordingSessionFactory(session)).record(
            context=context,
            request_id="req-1",
            channel=_channel(),
            requested_model="model-a",
            provider_model="provider-model-a",
            request_protocol="openai_chat",
            outcome="success",
        )
    )

    attempt = session.added[0]
    assert recorded is True
    assert session.commits == 1
    assert isinstance(attempt, RequestAttempt)
    assert attempt.attempt_index == 1
    assert attempt.request_origin == "rotor_agent"
    assert attempt.agent_run_id == "run-1"
    assert attempt.outcome == "success"
    assert attempt.sanitized_error is None


def test_record_attempt_persists_normalized_failure() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/chat")
    response = httpx.Response(
        503,
        request=request,
        headers={"x-request-id": "provider-1"},
        json={"error": {"message": "unavailable"}},
    )
    error = normalize_upstream_error(
        httpx.HTTPStatusError(
            "unavailable",
            request=request,
            response=response,
        )
    )
    session = RecordingSession()

    asyncio.run(
        AttemptRecorder(RecordingSessionFactory(session)).record(
            context=AttemptContext.start(0),
            request_id="req-1",
            channel=_channel(),
            requested_model="model-a",
            provider_model="provider-model-a",
            request_protocol="openai_chat",
            outcome="failed",
            error=error,
        )
    )

    attempt = session.added[0]
    assert attempt.upstream_status == 503
    assert attempt.error_category == "upstream_availability"
    assert attempt.error_code == "upstream_unavailable"
    assert attempt.retry_same_channel is True
    assert attempt.fallback_allowed is True
    assert attempt.provider_request_ids == {"x-request-id": "provider-1"}


def test_record_attempt_marks_context_only_after_commit() -> None:
    context = AttemptContext.start(0)
    recorder = AttemptRecorder(
        RecordingSessionFactory(RecordingSession(fail_commit=True))
    )

    try:
        asyncio.run(
            recorder.record(
                context=context,
                request_id="req-1",
                channel=_channel(),
                requested_model="model-a",
                provider_model="provider-model-a",
                request_protocol="openai_chat",
                outcome="success",
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "commit failed"
    else:
        raise AssertionError("commit failure should propagate")

    assert context.recorded is False


def test_record_attempt_ignores_duplicate_terminal_write() -> None:
    session = RecordingSession()
    factory = RecordingSessionFactory(session)
    context = AttemptContext.start(0)
    recorder = AttemptRecorder(factory)
    arguments = {
        "context": context,
        "request_id": "req-1",
        "channel": _channel(),
        "requested_model": "model-a",
        "provider_model": "provider-model-a",
        "request_protocol": "openai_chat",
        "outcome": "success",
    }

    assert asyncio.run(recorder.record(**arguments)) is True
    assert asyncio.run(recorder.record(**arguments)) is False

    assert factory.calls == 1
    assert len(session.added) == 1


def test_attempt_survives_unrelated_request_transaction_rollback(tmp_path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}"
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(RequestAttempt.__table__.create)

        request_session = factory()
        request_session.add(
            RequestAttempt(
                request_id="req-rolled-back",
                attempt_index=0,
                channel_id=12,
                requested_model="model-a",
                provider_model="provider-model-a",
                request_protocol="openai_chat",
                provider_protocol="openai",
                outcome="success",
                provider_request_ids={},
                request_origin="client",
            )
        )
        recorder = AttemptRecorder(factory)
        await recorder.record(
            context=AttemptContext.start(0),
            request_id="req-independent",
            channel=_channel(),
            requested_model="model-a",
            provider_model="provider-model-a",
            request_protocol="openai_chat",
            outcome="success",
        )
        await request_session.rollback()
        await request_session.close()

        async with factory() as verification_session:
            attempt = await verification_session.scalar(
                select(RequestAttempt).where(
                    RequestAttempt.request_id == "req-independent"
                )
            )
            rolled_back = await verification_session.scalar(
                select(RequestAttempt).where(
                    RequestAttempt.request_id == "req-rolled-back"
                )
            )
        await engine.dispose()

        assert attempt is not None
        assert attempt.outcome == "success"
        assert rolled_back is None

    asyncio.run(exercise())


def test_database_constraint_makes_fresh_context_idempotent(tmp_path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'idempotence.db'}"
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(RequestAttempt.__table__.create)
        recorder = AttemptRecorder(factory)
        arguments = {
            "request_id": "req-duplicate",
            "channel": _channel(),
            "requested_model": "model-a",
            "provider_model": "provider-model-a",
            "request_protocol": "openai_chat",
            "outcome": "success",
        }

        first = await recorder.record(
            context=AttemptContext.start(0),
            **arguments,
        )
        second_context = AttemptContext.start(0)
        second = await recorder.record(
            context=second_context,
            **arguments,
        )

        async with factory() as verification_session:
            attempts = list(
                (
                    await verification_session.scalars(
                        select(RequestAttempt).where(
                            RequestAttempt.request_id == "req-duplicate"
                        )
                    )
                ).all()
            )
        await engine.dispose()

        assert first is True
        assert second is False
        assert second_context.recorded is True
        assert len(attempts) == 1

    asyncio.run(exercise())
