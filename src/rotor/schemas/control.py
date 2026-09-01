from datetime import datetime, timezone

from pydantic import BaseModel, Field

from rotor.schemas.request_trace import RequestTrace


class ControlResponseMeta(BaseModel):
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    freshness: str = "persisted"
    redactions: list[str] = Field(default_factory=list)


class ChannelSecretState(BaseModel):
    has_key: bool


class ControlChannel(BaseModel):
    id: int
    name: str
    type: str
    base_url_origin: str | None = None
    protocol: str
    models: list[str] = Field(default_factory=list)
    model_mapping: dict[str, str] = Field(default_factory=dict)
    priority: int
    weight: int
    enabled: bool
    test_only: bool
    cache_scope: str
    capacity_scope: str
    billing_scope: str
    secret_state: ChannelSecretState
    updated_at: datetime | None = None


class ChannelControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: ControlChannel
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)


class ChannelListControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: list[ControlChannel] = Field(default_factory=list)
    page: "ControlPage"
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)


class RequestTraceControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: RequestTrace
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)


class FailureGroupKey(BaseModel):
    model: str | None = None
    channel_id: int | None = None
    category: str | None = None
    status: int | None = None


class RecentFailureGroup(BaseModel):
    group: FailureGroupKey
    count: int
    latest_at: datetime
    sample_request_ids: list[str] = Field(default_factory=list)


class ControlPage(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False
    limit: int


class RecentFailuresControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: list[RecentFailureGroup] = Field(default_factory=list)
    page: ControlPage
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)


class ModelUsageSummary(BaseModel):
    model: str
    request_count: int
    ledger_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    uncached_input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    usage_v2_ledger_count: int
    costed_ledger_count: int
    cost_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    reasoning_tokens: int
    input_audio_tokens: int
    output_audio_tokens: int


class ControlTimeWindow(BaseModel):
    start_time: datetime
    end_time: datetime
    end_exclusive: bool = True


class ModelUsageControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: list[ModelUsageSummary] = Field(default_factory=list)
    window: ControlTimeWindow
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)


class SessionLeaseEvaluationCohort(BaseModel):
    channel_id: int | None = None
    tariff_version: str | None = None
    tariff_period: str | None = None
    currency: str
    ledger_count: int
    prompt_tokens: int
    completion_tokens: int
    uncached_input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    cache_read_rate: float | None = None
    cache_write_rate: float | None = None
    input_cost: float
    output_cost: float
    total_cost: float


class SessionLeaseEvaluationSummary(BaseModel):
    model: str | None = None
    routing_decision_count: int
    stable_session_decision_count: int
    stable_session_coverage_rate: float | None = None
    lease_preferred_decision_count: int
    lease_applied_decision_count: int
    lease_application_rate: float | None = None
    lease_event_counts: dict[str, int] = Field(default_factory=dict)
    continuation_count: int
    migration_rate: float | None = None
    fallback_migration_count: int
    success_usage_ledger_count: int
    provider_usage_coverage_rate: float | None = None
    usage_v2_coverage_rate: float | None = None
    cost_coverage_rate: float | None = None
    prompt_tokens: int
    uncached_input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    uncached_input_rate: float | None = None
    cache_read_rate: float | None = None
    cache_write_rate: float | None = None
    cost_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    costed_cohorts: list[SessionLeaseEvaluationCohort] = Field(
        default_factory=list
    )
    attempt_request_count: int
    fallback_request_count: int
    fallback_success_count: int
    fallback_success_rate: float | None = None
    rate_limited_attempt_count: int
    server_error_attempt_count: int
    facts_complete_for_evaluation: bool
    blocking_reasons: list[str] = Field(default_factory=list)


class SessionLeaseEvaluationControlResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: SessionLeaseEvaluationSummary
    window: ControlTimeWindow
    meta: ControlResponseMeta = Field(default_factory=ControlResponseMeta)
