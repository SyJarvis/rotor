from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCategory(str, Enum):
    AUTHENTICATION_OR_PERMISSION = "authentication_or_permission"
    QUOTA_OR_RATE_LIMIT = "quota_or_rate_limit"
    PROTOCOL_OR_PARAMETER_ERROR = "protocol_or_parameter_error"
    MODEL_NOT_FOUND = "model_not_found"
    UPSTREAM_AVAILABILITY = "upstream_availability"
    NETWORK_CONNECTIVITY = "network_connectivity"
    TIMEOUT = "timeout"
    STREAM_INTERRUPTED = "stream_interrupted"
    ROUTING_NO_CANDIDATE = "routing_no_candidate"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


class ErrorPhase(str, Enum):
    ROUTING = "routing"
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_STREAM = "provider_stream"
    ACCOUNTING = "accounting"
    INTERNAL = "internal"


class UpstreamErrorFact(BaseModel):
    """Bounded, sanitized error fact safe for persistence and diagnostics."""

    schema_version: str = "1"
    code: str
    category: ErrorCategory
    phase: ErrorPhase
    upstream_status: int | None = None
    # ``retryable`` is retained for schema-v1 compatibility. New routing and
    # agent consumers should use the two explicit policy inputs below.
    retryable: bool
    retry_same_channel: bool
    fallback_allowed: bool
    retry_after_seconds: float | None = None
    message: str
    sanitized_body: Any | None = None
    provider_request_ids: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
