"""Durable persistence for one upstream channel attempt."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from rotor.database import _cancellation_safe
from rotor.gateway.routing import AttemptAdmission
from rotor.models.channel import Channel
from rotor.models.request_attempt import RequestAttempt
from rotor.schemas.error import UpstreamErrorFact


def _session_factory():
    from rotor.database import async_session_maker

    return async_session_maker


@dataclass(slots=True)
class AttemptContext:
    attempt_index: int
    started_at: datetime
    started_monotonic: float
    request_origin: str = "client"
    agent_run_id: str | None = None
    recorded: bool = field(default=False, init=False)
    admission: AttemptAdmission | None = field(default=None, init=False, repr=False)

    @classmethod
    def start(
        cls,
        attempt_index: int,
        *,
        request_origin: str = "client",
        agent_run_id: str | None = None,
    ) -> "AttemptContext":
        return cls(
            attempt_index=attempt_index,
            started_at=datetime.now(timezone.utc),
            started_monotonic=time.monotonic(),
            request_origin=request_origin,
            agent_run_id=agent_run_id,
        )

    def elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started_monotonic) * 1000))


class AttemptRecorder:
    """Persist attempt facts, sharing the request transaction when possible."""

    def __init__(self, session_factory: Any = None) -> None:
        # Resolve lazily so tests can monkeypatch rotor.database state.
        self._session_factory = session_factory

    def build_attempt(
        self,
        *,
        context,
        request_id: str,
        channel: Channel,
        requested_model: str,
        provider_model: str | None,
        request_protocol: str,
        outcome: str,
        error=None,
    ):
        """Build the attempt ORM object without touching the database.

        Lets request handlers persist the attempt in their own transaction
        instead of opening a second session + commit on the hot path.
        """
        from rotor.models.request_attempt import RequestAttempt

        return RequestAttempt(
            request_id=request_id,
            attempt_index=context.attempt_index,
            channel_id=channel.id,
            requested_model=requested_model,
            provider_model=provider_model,
            request_protocol=request_protocol,
            provider_protocol=channel.protocol,
            started_at=context.started_at,
            finished_at=datetime.now(timezone.utc),
            latency_ms=context.elapsed_ms(),
            outcome=outcome,
            upstream_status=error.upstream_status if error else None,
            error_category=error.category.value if error else None,
            error_code=error.code if error else None,
            retryable=error.retryable if error else None,
            retry_same_channel=error.retry_same_channel if error else None,
            fallback_allowed=error.fallback_allowed if error else None,
            retry_after_seconds=error.retry_after_seconds if error else None,
            sanitized_error=error.model_dump(mode="json") if error else None,
            provider_request_ids=error.provider_request_ids if error else {},
            request_origin=context.request_origin,
            agent_run_id=context.agent_run_id,
        )

    async def record(
        self,
        *,
        context: AttemptContext,
        request_id: str,
        channel: Channel,
        requested_model: str,
        provider_model: str | None,
        request_protocol: str,
        outcome: str,
        error: UpstreamErrorFact | None = None,
        db=None,
    ) -> bool:
        if context.recorded:
            return False

        attempt = self.build_attempt(
            context=context,
            request_id=request_id,
            channel=channel,
            requested_model=requested_model,
            provider_model=provider_model,
            request_protocol=request_protocol,
            outcome=outcome,
            error=error,
        )

        if db is not None:
            # Request/streaming accounting path: join the caller's
            # transaction so the attempt shares one commit with usage
            # accounting. Fake sessions in unit tests may not implement
            # savepoints; fall back to plain add+flush then.
            if hasattr(db, "begin_nested"):
                try:
                    async with db.begin_nested():
                        db.add(attempt)
                        await db.flush()
                except IntegrityError:
                    context.recorded = True
                    return False
            else:
                db.add(attempt)
                try:
                    await db.flush()
                except IntegrityError:
                    context.recorded = True
                    return False
            context.recorded = True
            return True

        async with (self._session_factory or _session_factory())() as session:
            session.add(attempt)
            try:
                await _cancellation_safe(session.commit())
            except IntegrityError:
                await _cancellation_safe(session.rollback())
                existing = await session.scalar(
                    select(RequestAttempt.id).where(
                        RequestAttempt.request_id == request_id,
                        RequestAttempt.attempt_index == context.attempt_index,
                    )
                )
                if existing is None:
                    raise
                context.recorded = True
                return False

        context.recorded = True
        return True


attempt_recorder = AttemptRecorder()
