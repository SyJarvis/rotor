from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.models.request_attempt import RequestAttempt
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLeaseEvent
from rotor.models.usage import UsageLedger
from rotor.schemas.control import (
    SessionLeaseEvaluationCohort,
    SessionLeaseEvaluationSummary,
)


@dataclass(frozen=True)
class SessionLeaseEvaluationResult:
    summary: SessionLeaseEvaluationSummary
    start_time: datetime
    end_time: datetime


async def evaluate_session_leases(
    db: AsyncSession,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    model: str | None = None,
) -> SessionLeaseEvaluationResult:
    effective_end = _as_utc(end_time) if end_time else datetime.now(
        timezone.utc
    )
    effective_start = (
        _as_utc(start_time)
        if start_time
        else effective_end - timedelta(hours=24)
    )
    _validate_window(effective_start, effective_end)

    routing_query = select(RoutingDecisionRecord.feature_snapshot).where(
        RoutingDecisionRecord.created_at >= effective_start,
        RoutingDecisionRecord.created_at < effective_end,
    )
    if model is not None:
        routing_query = routing_query.where(
            RoutingDecisionRecord.model == model
        )
    routing_features = list((await db.scalars(routing_query)).all())
    stable_session_decisions = 0
    lease_preferred_decisions = 0
    lease_applied_decisions = 0
    for raw_features in routing_features:
        features = raw_features if isinstance(raw_features, dict) else {}
        if features.get("session_source"):
            stable_session_decisions += 1
        lease = features.get("session_lease")
        if not isinstance(lease, dict):
            continue
        if lease.get("preferred_channel_id") is not None:
            lease_preferred_decisions += 1
        if lease.get("used") is True:
            lease_applied_decisions += 1

    event_query = (
        select(
            SessionLeaseEvent.event_type,
            SessionLeaseEvent.reason,
            func.count(SessionLeaseEvent.id).label("event_count"),
        )
        .where(
            SessionLeaseEvent.created_at >= effective_start,
            SessionLeaseEvent.created_at < effective_end,
        )
        .group_by(SessionLeaseEvent.event_type, SessionLeaseEvent.reason)
    )
    if model is not None:
        event_query = event_query.where(
            SessionLeaseEvent.logical_model == model
        )
    event_rows = (await db.execute(event_query)).all()
    event_counts = {
        event_type: 0
        for event_type in ("assigned", "renewed", "migrated", "expired")
    }
    fallback_migration_count = 0
    for row in event_rows:
        event_counts[row.event_type] = (
            event_counts.get(row.event_type, 0) + int(row.event_count or 0)
        )
        if row.event_type == "migrated" and row.reason == "fallback_success":
            fallback_migration_count += int(row.event_count or 0)

    usage_filters = [
        UsageLedger.created_at >= effective_start,
        UsageLedger.created_at < effective_end,
        UsageLedger.status == "success",
    ]
    if model is not None:
        usage_filters.append(UsageLedger.model == model)
    usage_row = (
        await db.execute(
            select(
                func.count(UsageLedger.id).label("ledger_count"),
                func.sum(case(
                    (UsageLedger.usage_source == "provider", 1),
                    else_=0,
                )).label("provider_usage_count"),
                func.sum(case(
                    (UsageLedger.usage_schema_version == "2", 1),
                    else_=0,
                )).label("usage_v2_count"),
                func.sum(case(
                    (UsageLedger.cost_status == "calculated", 1),
                    else_=0,
                )).label("costed_count"),
                func.sum(UsageLedger.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageLedger.uncached_input_tokens).label(
                    "uncached_input_tokens"
                ),
                func.sum(UsageLedger.cached_tokens).label("cached_tokens"),
                func.sum(UsageLedger.cache_write_tokens).label(
                    "cache_write_tokens"
                ),
            ).where(*usage_filters)
        )
    ).one()
    usage_ledger_count = int(usage_row.ledger_count or 0)
    provider_usage_count = int(usage_row.provider_usage_count or 0)
    usage_v2_count = int(usage_row.usage_v2_count or 0)
    costed_count = int(usage_row.costed_count or 0)
    prompt_tokens = int(usage_row.prompt_tokens or 0)
    uncached_input_tokens = int(usage_row.uncached_input_tokens or 0)
    cached_tokens = int(usage_row.cached_tokens or 0)
    cache_write_tokens = int(usage_row.cache_write_tokens or 0)

    cost_rows = (
        await db.execute(
            select(
                UsageLedger.currency,
                func.sum(UsageLedger.total_cost).label("total_cost"),
            )
            .where(
                *usage_filters,
                UsageLedger.cost_status == "calculated",
            )
            .group_by(UsageLedger.currency)
        )
    ).all()
    cost_totals_by_currency = {
        row.currency: float(row.total_cost or 0.0)
        for row in cost_rows
        if row.currency
    }
    cohort_rows = (
        await db.execute(
            select(
                UsageLedger.channel_id,
                UsageLedger.tariff_version,
                UsageLedger.tariff_period,
                UsageLedger.currency,
                func.count(UsageLedger.id).label("ledger_count"),
                func.sum(UsageLedger.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageLedger.completion_tokens).label(
                    "completion_tokens"
                ),
                func.sum(UsageLedger.uncached_input_tokens).label(
                    "uncached_input_tokens"
                ),
                func.sum(UsageLedger.cached_tokens).label("cached_tokens"),
                func.sum(UsageLedger.cache_write_tokens).label(
                    "cache_write_tokens"
                ),
                func.sum(UsageLedger.input_cost).label("input_cost"),
                func.sum(UsageLedger.output_cost).label("output_cost"),
                func.sum(UsageLedger.total_cost).label("total_cost"),
            )
            .where(
                *usage_filters,
                UsageLedger.cost_status == "calculated",
            )
            .group_by(
                UsageLedger.channel_id,
                UsageLedger.tariff_version,
                UsageLedger.tariff_period,
                UsageLedger.currency,
            )
            .order_by(
                UsageLedger.channel_id,
                UsageLedger.tariff_period,
                UsageLedger.currency,
            )
        )
    ).all()
    costed_cohorts = [
        _costed_cohort(row)
        for row in cohort_rows
        if row.currency
    ]

    attempt_filters = [
        RequestAttempt.started_at >= effective_start,
        RequestAttempt.started_at < effective_end,
    ]
    if model is not None:
        attempt_filters.append(RequestAttempt.requested_model == model)
    attempt_row = (
        await db.execute(
            select(
                func.count(distinct(RequestAttempt.request_id)).label(
                    "request_count"
                ),
                func.count(distinct(case(
                    (RequestAttempt.attempt_index > 0, RequestAttempt.request_id),
                    else_=None,
                ))).label("fallback_request_count"),
                func.count(distinct(case(
                    (
                        (RequestAttempt.attempt_index > 0)
                        & (RequestAttempt.outcome == "success"),
                        RequestAttempt.request_id,
                    ),
                    else_=None,
                ))).label("fallback_success_count"),
                func.sum(case(
                    (RequestAttempt.upstream_status == 429, 1),
                    else_=0,
                )).label("rate_limited_attempt_count"),
                func.sum(case(
                    (RequestAttempt.upstream_status >= 500, 1),
                    else_=0,
                )).label("server_error_attempt_count"),
            ).where(*attempt_filters)
        )
    ).one()
    attempt_request_count = int(attempt_row.request_count or 0)
    fallback_request_count = int(attempt_row.fallback_request_count or 0)
    fallback_success_count = int(attempt_row.fallback_success_count or 0)

    continuation_count = event_counts["renewed"] + event_counts["migrated"]
    blocking_reasons = _evaluation_blockers(
        routing_decision_count=len(routing_features),
        stable_session_decision_count=stable_session_decisions,
        lease_event_count=sum(event_counts.values()),
        usage_ledger_count=usage_ledger_count,
        provider_usage_count=provider_usage_count,
        usage_v2_count=usage_v2_count,
        costed_count=costed_count,
        currencies=set(cost_totals_by_currency),
    )
    summary = SessionLeaseEvaluationSummary(
        model=model,
        routing_decision_count=len(routing_features),
        stable_session_decision_count=stable_session_decisions,
        stable_session_coverage_rate=_ratio(
            stable_session_decisions, len(routing_features)
        ),
        lease_preferred_decision_count=lease_preferred_decisions,
        lease_applied_decision_count=lease_applied_decisions,
        lease_application_rate=_ratio(
            lease_applied_decisions, lease_preferred_decisions
        ),
        lease_event_counts=event_counts,
        continuation_count=continuation_count,
        migration_rate=_ratio(
            event_counts["migrated"], continuation_count
        ),
        fallback_migration_count=fallback_migration_count,
        success_usage_ledger_count=usage_ledger_count,
        provider_usage_coverage_rate=_ratio(
            provider_usage_count, usage_ledger_count
        ),
        usage_v2_coverage_rate=_ratio(
            usage_v2_count, usage_ledger_count
        ),
        cost_coverage_rate=_ratio(costed_count, usage_ledger_count),
        prompt_tokens=prompt_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        uncached_input_rate=_ratio(uncached_input_tokens, prompt_tokens),
        cache_read_rate=_ratio(cached_tokens, prompt_tokens),
        cache_write_rate=_ratio(cache_write_tokens, prompt_tokens),
        cost_totals_by_currency=cost_totals_by_currency,
        costed_cohorts=costed_cohorts,
        attempt_request_count=attempt_request_count,
        fallback_request_count=fallback_request_count,
        fallback_success_count=fallback_success_count,
        fallback_success_rate=_ratio(
            fallback_success_count, fallback_request_count
        ),
        rate_limited_attempt_count=int(
            attempt_row.rate_limited_attempt_count or 0
        ),
        server_error_attempt_count=int(
            attempt_row.server_error_attempt_count or 0
        ),
        facts_complete_for_evaluation=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )
    return SessionLeaseEvaluationResult(
        summary=summary,
        start_time=effective_start,
        end_time=effective_end,
    )


def _evaluation_blockers(
    *,
    routing_decision_count: int,
    stable_session_decision_count: int,
    lease_event_count: int,
    usage_ledger_count: int,
    provider_usage_count: int,
    usage_v2_count: int,
    costed_count: int,
    currencies: set[str],
) -> list[str]:
    blockers: list[str] = []
    if routing_decision_count == 0:
        blockers.append("no_routing_decisions")
    if stable_session_decision_count == 0:
        blockers.append("no_stable_session_decisions")
    if lease_event_count == 0:
        blockers.append("no_session_lease_events")
    if usage_ledger_count == 0:
        blockers.append("no_success_usage")
    else:
        if provider_usage_count != usage_ledger_count:
            blockers.append("incomplete_provider_usage")
        if usage_v2_count != usage_ledger_count:
            blockers.append("incomplete_usage_v2")
        if costed_count != usage_ledger_count:
            blockers.append("incomplete_cost_facts")
    if len(currencies) > 1:
        blockers.append("multiple_currencies")
    return blockers


def _costed_cohort(row) -> SessionLeaseEvaluationCohort:
    prompt_tokens = int(row.prompt_tokens or 0)
    cached_tokens = int(row.cached_tokens or 0)
    cache_write_tokens = int(row.cache_write_tokens or 0)
    return SessionLeaseEvaluationCohort(
        channel_id=row.channel_id,
        tariff_version=row.tariff_version,
        tariff_period=row.tariff_period,
        currency=row.currency,
        ledger_count=int(row.ledger_count or 0),
        prompt_tokens=prompt_tokens,
        completion_tokens=int(row.completion_tokens or 0),
        uncached_input_tokens=int(row.uncached_input_tokens or 0),
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_rate=_ratio(cached_tokens, prompt_tokens),
        cache_write_rate=_ratio(cache_write_tokens, prompt_tokens),
        input_cost=float(row.input_cost or 0.0),
        output_cost=float(row.output_cost or 0.0),
        total_cost=float(row.total_cost or 0.0),
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _validate_window(start_time: datetime, end_time: datetime) -> None:
    if start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")
    if end_time - start_time > timedelta(days=30):
        raise ValueError(
            "session lease evaluation window must not exceed 30 days"
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
