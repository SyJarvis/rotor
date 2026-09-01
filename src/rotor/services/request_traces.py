from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.conversation import ConversationRecord
from rotor.models.request_attempt import RequestAttempt
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLeaseEvent
from rotor.models.usage import UsageLedger
from rotor.schemas.request_trace import (
    AttemptErrorTrace,
    RequestAttemptTrace,
    RequestTrace,
    RequestTraceMeta,
    RoutingDecisionTrace,
    SessionLeaseEventTrace,
    UsageTrace,
)


async def get_request_trace(
    db: AsyncSession,
    request_id: str,
) -> RequestTrace | None:
    routing_records = list(
        (
            await db.scalars(
                select(RoutingDecisionRecord)
                .where(RoutingDecisionRecord.request_id == request_id)
                .order_by(
                    RoutingDecisionRecord.created_at,
                    RoutingDecisionRecord.id,
                )
            )
        ).all()
    )
    attempts = list(
        (
            await db.scalars(
                select(RequestAttempt)
                .where(RequestAttempt.request_id == request_id)
                .order_by(
                    RequestAttempt.attempt_index,
                    RequestAttempt.id,
                )
            )
        ).all()
    )
    usage_records = list(
        (
            await db.scalars(
                select(UsageLedger)
                .where(UsageLedger.request_id == request_id)
                .order_by(UsageLedger.created_at, UsageLedger.id)
            )
        ).all()
    )
    conversation_records = list(
        (
            await db.scalars(
                select(ConversationRecord)
                .where(ConversationRecord.request_id == request_id)
                .order_by(
                    ConversationRecord.updated_at,
                    ConversationRecord.id,
                )
            )
        ).all()
    )
    session_lease_events = list(
        (
            await db.scalars(
                select(SessionLeaseEvent)
                .where(SessionLeaseEvent.request_id == request_id)
                .order_by(
                    SessionLeaseEvent.created_at,
                    SessionLeaseEvent.id,
                )
            )
        ).all()
    )

    if not any(
        (
            routing_records,
            attempts,
            usage_records,
            conversation_records,
            session_lease_events,
        )
    ):
        return None

    warnings: list[str] = []
    routing_record = routing_records[0] if routing_records else None
    if routing_record is None:
        warnings.append("missing_routing_decision")
    elif len(routing_records) > 1:
        warnings.append("multiple_routing_decisions")

    if not attempts:
        warnings.append("missing_attempts")
    if not usage_records:
        warnings.append("missing_usage")
    if not conversation_records:
        warnings.append("missing_conversation_record")
    elif len(conversation_records) > 1:
        warnings.append("multiple_conversation_records")

    attempt_traces = [
        _attempt_trace(attempt, warnings)
        for attempt in attempts
    ]
    routing_trace = (
        _routing_trace(routing_record)
        if routing_record is not None
        else None
    )
    usage_trace = _usage_trace(usage_records, warnings)

    requested_models = {
        value
        for value in (
            routing_record.model if routing_record else None,
            *(attempt.requested_model for attempt in attempts),
            *(record.model for record in usage_records),
            *(record.model for record in conversation_records),
        )
        if value is not None
    }
    request_protocols = {
        value
        for value in (
            routing_record.request_protocol if routing_record else None,
            *(attempt.request_protocol for attempt in attempts),
            *(record.request_protocol for record in usage_records),
            *(record.protocol for record in conversation_records),
        )
        if value is not None
    }
    if len(requested_models) > 1:
        warnings.append("inconsistent_requested_model")
    if len(request_protocols) > 1:
        warnings.append("inconsistent_request_protocol")
    origins = {attempt.request_origin for attempt in attempts}
    agent_run_ids = {
        attempt.agent_run_id
        for attempt in attempts
        if attempt.agent_run_id is not None
    }
    if len(origins) > 1:
        warnings.append("inconsistent_request_origin")
    if len(agent_run_ids) > 1:
        warnings.append("inconsistent_agent_run_id")

    started_at = _minimum_datetime(
        [
            *(attempt.started_at for attempt in attempts),
            *(record.created_at for record in routing_records),
            *(record.created_at for record in usage_records),
            *(record.created_at for record in conversation_records),
            *(event.created_at for event in session_lease_events),
        ]
    )
    finished_at = _maximum_datetime(
        [
            *(
                attempt.finished_at
                for attempt in attempts
                if attempt.finished_at is not None
            ),
            *(record.created_at for record in usage_records),
            *(record.updated_at for record in conversation_records),
            *(event.created_at for event in session_lease_events),
        ]
    )

    return RequestTrace(
        request_id=request_id,
        origin=next(iter(origins)) if len(origins) == 1 else None,
        agent_run_id=(
            next(iter(agent_run_ids))
            if len(agent_run_ids) == 1
            else None
        ),
        requested_model=(
            next(iter(requested_models))
            if len(requested_models) == 1
            else None
        ),
        request_protocol=(
            next(iter(request_protocols))
            if len(request_protocols) == 1
            else None
        ),
        routing_decision=routing_trace,
        attempts=attempt_traces,
        session_lease_events=[
            SessionLeaseEventTrace(
                event_type=event.event_type,
                previous_channel_id=event.previous_channel_id,
                channel_id=event.channel_id,
                reason=event.reason,
                created_at=event.created_at,
            )
            for event in session_lease_events
        ],
        final_outcome=_final_outcome(
            attempts,
            usage_records,
            conversation_records,
            warnings,
        ),
        usage=usage_trace,
        started_at=started_at,
        finished_at=finished_at,
        meta=RequestTraceMeta(warnings=warnings),
    )


def _routing_trace(record: RoutingDecisionRecord) -> RoutingDecisionTrace:
    return RoutingDecisionTrace(
        strategy=record.strategy,
        policy_version=record.policy_version,
        candidate_channel_ids=record.candidate_channel_ids or [],
        selected_channel_id=record.selected_channel_id,
        required_capabilities=record.required_capabilities or [],
        affinity_used=record.affinity_used,
        feature_snapshot=record.feature_snapshot or {},
        score_snapshot=record.score_snapshot,
        created_at=record.created_at,
    )


def _attempt_trace(
    attempt: RequestAttempt,
    warnings: list[str],
) -> RequestAttemptTrace:
    raw_error = attempt.sanitized_error or {}
    has_error = any(
        (
            raw_error,
            attempt.upstream_status is not None,
            attempt.error_category is not None,
            attempt.error_code is not None,
        )
    )
    error = None
    if has_error:
        error = AttemptErrorTrace(
            code=attempt.error_code or raw_error.get("code"),
            category=attempt.error_category or raw_error.get("category"),
            phase=raw_error.get("phase"),
            upstream_status=(
                attempt.upstream_status
                if attempt.upstream_status is not None
                else raw_error.get("upstream_status")
            ),
            retryable=(
                attempt.retryable
                if attempt.retryable is not None
                else raw_error.get("retryable")
            ),
            retry_same_channel=(
                attempt.retry_same_channel
                if attempt.retry_same_channel is not None
                else raw_error.get("retry_same_channel")
            ),
            fallback_allowed=(
                attempt.fallback_allowed
                if attempt.fallback_allowed is not None
                else raw_error.get("fallback_allowed")
            ),
            retry_after_seconds=(
                attempt.retry_after_seconds
                if attempt.retry_after_seconds is not None
                else raw_error.get("retry_after_seconds")
            ),
            message=raw_error.get("message"),
            sanitized_body=raw_error.get("sanitized_body"),
            provider_request_ids=(
                attempt.provider_request_ids
                or raw_error.get("provider_request_ids")
                or {}
            ),
            occurred_at=raw_error.get("occurred_at"),
        )
    elif attempt.outcome == "failed":
        warnings.append(
            f"attempt_{attempt.attempt_index}_missing_error_fact"
        )

    return RequestAttemptTrace(
        id=f"attempt_{attempt.id}",
        attempt_index=attempt.attempt_index,
        channel_id=attempt.channel_id,
        provider_model=attempt.provider_model,
        provider_protocol=attempt.provider_protocol,
        outcome=attempt.outcome,
        upstream_status=attempt.upstream_status,
        error=error,
        latency_ms=attempt.latency_ms,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def _usage_trace(
    records: list[UsageLedger],
    warnings: list[str],
) -> UsageTrace | None:
    if not records:
        return None
    currencies = {
        record.currency
        for record in records
        if record.cost_status == "calculated" and record.currency
    }
    if len(currencies) > 1:
        warnings.append("multiple_usage_currencies")
    return UsageTrace(
        ledger_count=len(records),
        prompt_tokens=sum(record.prompt_tokens or 0 for record in records),
        completion_tokens=sum(
            record.completion_tokens or 0 for record in records
        ),
        total_tokens=sum(record.total_tokens or 0 for record in records),
        uncached_input_tokens=sum(
            record.uncached_input_tokens or 0 for record in records
        ),
        cached_tokens=sum(record.cached_tokens or 0 for record in records),
        cache_write_tokens=sum(
            record.cache_write_tokens or 0 for record in records
        ),
        cache_write_5m_tokens=sum(
            record.cache_write_5m_tokens or 0 for record in records
        ),
        cache_write_1h_tokens=sum(
            record.cache_write_1h_tokens or 0 for record in records
        ),
        reasoning_tokens=sum(
            record.reasoning_tokens or 0 for record in records
        ),
        input_audio_tokens=sum(
            record.input_audio_tokens or 0 for record in records
        ),
        output_audio_tokens=sum(
            record.output_audio_tokens or 0 for record in records
        ),
        input_cost=sum(record.input_cost or 0.0 for record in records),
        output_cost=sum(record.output_cost or 0.0 for record in records),
        total_cost=sum(record.total_cost or 0.0 for record in records),
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
        usage_sources=sorted(
            {record.usage_source for record in records if record.usage_source}
        ),
        usage_schema_versions=sorted({
            record.usage_schema_version
            for record in records
            if record.usage_schema_version
        }),
        capacity_snapshots=[
            record.capacity_snapshot
            for record in records
            if record.capacity_snapshot
        ],
        cache_scopes=sorted({
            record.cache_scope for record in records if record.cache_scope
        }),
        capacity_scopes=sorted({
            record.capacity_scope
            for record in records
            if record.capacity_scope
        }),
        billing_scopes=sorted({
            record.billing_scope for record in records if record.billing_scope
        }),
        cost_statuses=sorted({
            record.cost_status for record in records if record.cost_status
        }),
        tariff_versions=sorted({
            record.tariff_version
            for record in records
            if record.tariff_version
        }),
        tariff_periods=sorted({
            record.tariff_period
            for record in records
            if record.tariff_period
        }),
        tariff_snapshots=[
            record.tariff_snapshot
            for record in records
            if record.tariff_snapshot
        ],
    )


def _final_outcome(
    attempts: list[RequestAttempt],
    usage_records: list[UsageLedger],
    conversation_records: list[ConversationRecord],
    warnings: list[str],
) -> str | None:
    terminal_statuses = {
        status
        for status in (
            (
                conversation_records[-1].status
                if conversation_records
                else None
            ),
            usage_records[-1].status if usage_records else None,
            attempts[-1].outcome if attempts else None,
        )
        if status in {"success", "failed", "cancelled"}
    }
    if len(terminal_statuses) > 1:
        warnings.append("inconsistent_final_outcome")
    if conversation_records:
        status = conversation_records[-1].status
        if status in {"success", "failed", "cancelled"}:
            return status
        warnings.append("conversation_not_terminal")
    if usage_records:
        return usage_records[-1].status
    if attempts:
        return attempts[-1].outcome
    warnings.append("missing_final_outcome")
    return None


def _minimum_datetime(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _maximum_datetime(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None
