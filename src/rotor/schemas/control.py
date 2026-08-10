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
    cached_tokens: int
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
