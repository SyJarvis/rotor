from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RoutingDecisionTrace(BaseModel):
    strategy: str
    policy_version: str | None = None
    candidate_channel_ids: list[int] = Field(default_factory=list)
    selected_channel_id: int | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    affinity_used: bool = False
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)
    score_snapshot: dict[str, Any] | None = None
    created_at: datetime | None = None


class AttemptErrorTrace(BaseModel):
    code: str | None = None
    category: str | None = None
    phase: str | None = None
    upstream_status: int | None = None
    retryable: bool | None = None
    retry_same_channel: bool | None = None
    fallback_allowed: bool | None = None
    retry_after_seconds: float | None = None
    message: str | None = None
    sanitized_body: Any | None = Field(default=None, exclude=True)
    provider_request_ids: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class RequestAttemptTrace(BaseModel):
    id: str
    attempt_index: int
    channel_id: int
    provider_model: str | None = None
    provider_protocol: str | None = None
    outcome: str
    upstream_status: int | None = None
    error: AttemptErrorTrace | None = None
    latency_ms: int | None = None
    started_at: datetime
    finished_at: datetime | None = None


class UsageTrace(BaseModel):
    ledger_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    input_audio_tokens: int
    output_audio_tokens: int
    total_cost: float
    currency: str | None = None
    usage_sources: list[str] = Field(default_factory=list)


class RequestTraceMeta(BaseModel):
    warnings: list[str] = Field(default_factory=list)


class RequestTrace(BaseModel):
    schema_version: str = "1"
    request_id: str
    origin: str | None = None
    agent_run_id: str | None = None
    requested_model: str | None = None
    request_protocol: str | None = None
    routing_decision: RoutingDecisionTrace | None = None
    attempts: list[RequestAttemptTrace] = Field(default_factory=list)
    final_outcome: str | None = None
    usage: UsageTrace | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    meta: RequestTraceMeta = Field(default_factory=RequestTraceMeta)
