from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rotor_mcp.errors import InvalidControlAPIResponse


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlResponseMeta(StrictModel):
    observed_at: datetime | None = None
    freshness: str = "persisted"
    redactions: list[str] = Field(default_factory=list)


class ChannelSecretState(StrictModel):
    has_key: bool


class Channel(StrictModel):
    id: int = Field(ge=1)
    name: str
    type: str
    base_url_origin: str | None = None
    protocol: str
    models: list[str] = Field(default_factory=list)
    model_mapping: dict[str, str] = Field(default_factory=dict)
    priority: int
    weight: int = Field(ge=1)
    enabled: bool
    test_only: bool
    cache_scope: str | None = None
    capacity_scope: str | None = None
    billing_scope: str | None = None
    secret_state: ChannelSecretState
    updated_at: datetime | None = None


class ChannelResponse(StrictModel):
    schema_version: str
    request_id: str
    data: Channel
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "ChannelResponse":
        return _validate_response(cls, payload, "channel")


class FailureGroupKey(StrictModel):
    model: str | None = None
    channel_id: int | None = None
    category: str | None = None
    status: int | None = None


class RecentFailureGroup(StrictModel):
    group: FailureGroupKey
    count: int = Field(ge=0)
    latest_at: datetime
    sample_request_ids: list[str] = Field(default_factory=list)


class ControlPage(StrictModel):
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = Field(ge=1, le=100)


class ChannelListResponse(StrictModel):
    schema_version: str
    request_id: str
    data: list[Channel] = Field(default_factory=list)
    page: ControlPage
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "ChannelListResponse":
        return _validate_response(cls, payload, "channel list")


class RecentFailuresResponse(StrictModel):
    schema_version: str
    request_id: str
    data: list[RecentFailureGroup] = Field(default_factory=list)
    page: ControlPage
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "RecentFailuresResponse":
        return _validate_response(cls, payload, "recent failures")


class ModelUsageSummary(StrictModel):
    model: str
    request_count: int = Field(ge=0)
    ledger_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    uncached_input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    usage_v2_ledger_count: int = Field(default=0, ge=0)
    costed_ledger_count: int = Field(default=0, ge=0)
    cost_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    reasoning_tokens: int = Field(ge=0)
    input_audio_tokens: int = Field(ge=0)
    output_audio_tokens: int = Field(ge=0)


class ControlTimeWindow(StrictModel):
    start_time: datetime
    end_time: datetime
    end_exclusive: bool


class ModelUsageResponse(StrictModel):
    schema_version: str
    request_id: str
    data: list[ModelUsageSummary] = Field(default_factory=list)
    window: ControlTimeWindow
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "ModelUsageResponse":
        return _validate_response(cls, payload, "model usage")


class SessionLeaseEvaluationCohort(StrictModel):
    channel_id: int | None = Field(default=None, ge=1)
    tariff_version: str | None = None
    tariff_period: str | None = None
    currency: str
    ledger_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    uncached_input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cache_read_rate: float | None = Field(default=None, ge=0, le=1)
    cache_write_rate: float | None = Field(default=None, ge=0, le=1)
    input_cost: float = Field(ge=0)
    output_cost: float = Field(ge=0)
    total_cost: float = Field(ge=0)


class SessionLeaseEvaluation(StrictModel):
    model: str | None = None
    routing_decision_count: int = Field(ge=0)
    stable_session_decision_count: int = Field(ge=0)
    stable_session_coverage_rate: float | None = Field(
        default=None, ge=0, le=1
    )
    lease_preferred_decision_count: int = Field(ge=0)
    lease_applied_decision_count: int = Field(ge=0)
    lease_application_rate: float | None = Field(default=None, ge=0, le=1)
    lease_event_counts: dict[str, int] = Field(default_factory=dict)
    continuation_count: int = Field(ge=0)
    migration_rate: float | None = Field(default=None, ge=0, le=1)
    fallback_migration_count: int = Field(ge=0)
    success_usage_ledger_count: int = Field(ge=0)
    provider_usage_coverage_rate: float | None = Field(
        default=None, ge=0, le=1
    )
    usage_v2_coverage_rate: float | None = Field(
        default=None, ge=0, le=1
    )
    cost_coverage_rate: float | None = Field(default=None, ge=0, le=1)
    prompt_tokens: int = Field(ge=0)
    uncached_input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    uncached_input_rate: float | None = Field(default=None, ge=0, le=1)
    cache_read_rate: float | None = Field(default=None, ge=0, le=1)
    cache_write_rate: float | None = Field(default=None, ge=0, le=1)
    cost_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    costed_cohorts: list[SessionLeaseEvaluationCohort] = Field(
        default_factory=list
    )
    attempt_request_count: int = Field(ge=0)
    fallback_request_count: int = Field(ge=0)
    fallback_success_count: int = Field(ge=0)
    fallback_success_rate: float | None = Field(default=None, ge=0, le=1)
    rate_limited_attempt_count: int = Field(ge=0)
    server_error_attempt_count: int = Field(ge=0)
    facts_complete_for_evaluation: bool
    blocking_reasons: list[str] = Field(default_factory=list)


class SessionLeaseEvaluationResponse(StrictModel):
    schema_version: str
    request_id: str
    data: SessionLeaseEvaluation
    window: ControlTimeWindow
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "SessionLeaseEvaluationResponse":
        return _validate_response(cls, payload, "Session Lease evaluation")


class RoutingDecisionTrace(StrictModel):
    strategy: str
    policy_version: str | None = None
    candidate_channel_ids: list[int] = Field(default_factory=list)
    selected_channel_id: int | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    affinity_used: bool = False
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)
    score_snapshot: dict[str, Any] | None = None
    created_at: datetime | None = None


class AttemptErrorTrace(StrictModel):
    code: str | None = None
    category: str | None = None
    phase: str | None = None
    upstream_status: int | None = None
    retryable: bool | None = None
    retry_same_channel: bool | None = None
    fallback_allowed: bool | None = None
    retry_after_seconds: float | None = None
    message: str | None = None
    provider_request_ids: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class RequestAttemptTrace(StrictModel):
    id: str
    attempt_index: int = Field(ge=0)
    channel_id: int = Field(ge=1)
    provider_model: str | None = None
    provider_protocol: str | None = None
    outcome: str
    upstream_status: int | None = None
    error: AttemptErrorTrace | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime | None = None


class UsageTrace(StrictModel):
    ledger_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    uncached_input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(ge=0)
    input_audio_tokens: int = Field(ge=0)
    output_audio_tokens: int = Field(ge=0)
    total_cost: float = Field(ge=0)
    input_cost: float = Field(default=0, ge=0)
    output_cost: float = Field(default=0, ge=0)
    currency: str | None = None
    usage_sources: list[str] = Field(default_factory=list)
    usage_schema_versions: list[str] = Field(default_factory=list)
    capacity_snapshots: list[dict[str, str]] = Field(default_factory=list)
    cache_scopes: list[str] = Field(default_factory=list)
    capacity_scopes: list[str] = Field(default_factory=list)
    billing_scopes: list[str] = Field(default_factory=list)
    cost_statuses: list[str] = Field(default_factory=list)
    tariff_versions: list[str] = Field(default_factory=list)
    tariff_periods: list[str] = Field(default_factory=list)
    tariff_snapshots: list[dict[str, Any]] = Field(default_factory=list)


class SessionLeaseEventTrace(StrictModel):
    event_type: str
    previous_channel_id: int | None = None
    channel_id: int | None = None
    reason: str
    created_at: datetime


class RequestTraceMeta(StrictModel):
    warnings: list[str] = Field(default_factory=list)


class RequestTrace(StrictModel):
    schema_version: str = "1"
    request_id: str
    origin: str | None = None
    agent_run_id: str | None = None
    requested_model: str | None = None
    request_protocol: str | None = None
    routing_decision: RoutingDecisionTrace | None = None
    attempts: list[RequestAttemptTrace] = Field(default_factory=list)
    session_lease_events: list[SessionLeaseEventTrace] = Field(
        default_factory=list
    )
    final_outcome: str | None = None
    usage: UsageTrace | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    meta: RequestTraceMeta = Field(default_factory=RequestTraceMeta)


class RequestTraceResponse(StrictModel):
    schema_version: str
    request_id: str
    data: RequestTrace
    meta: ControlResponseMeta

    @classmethod
    def from_control_payload(
        cls,
        payload: dict[str, Any],
    ) -> "RequestTraceResponse":
        redacted = deepcopy(payload)
        meta = redacted.get("meta")
        redactions = None
        if isinstance(meta, dict):
            redactions = meta.setdefault("redactions", [])
        data = redacted.get("data")
        attempts = data.get("attempts", []) if isinstance(data, dict) else []
        if isinstance(attempts, list):
            for index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    continue
                error = attempt.get("error")
                if not isinstance(error, dict):
                    continue
                provider_body = error.pop("sanitized_body", None)
                if provider_body is not None:
                    path = (
                        f"data.attempts[{index}].error.sanitized_body"
                    )
                    if (
                        isinstance(redactions, list)
                        and path not in redactions
                    ):
                        redactions.append(path)
        return _validate_response(cls, redacted, "request trace")


def _validate_response(
    model: type[StrictModel],
    payload: dict[str, Any],
    response_name: str,
) -> Any:
    try:
        return model.model_validate(payload)
    except (AttributeError, TypeError, ValidationError) as exc:
        raise InvalidControlAPIResponse(
            "Rotor Control API returned a response that does not match "
            f"the {response_name} MCP output schema"
        ) from exc
