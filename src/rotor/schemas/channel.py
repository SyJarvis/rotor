from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime

from rotor.core.resource_scopes import normalize_scope_config


class ProbeModelsResponse(BaseModel):
    models: List[str]
    latency_ms: int
    raw_count: int = 0


def _normalize_extra(value: Dict[str, Any]) -> Dict[str, Any]:
    """Validate channel-level provider overrides while preserving extensions."""
    forbidden = {"oauth", "oauth_credentials", "access_token", "refresh_token", "oauth_access_token", "oauth_refresh_token"}
    def contains_credentials(item):
        if isinstance(item, dict):
            return bool(forbidden & item.keys()) or any(contains_credentials(child) for child in item.values())
        if isinstance(item, list):
            return any(contains_credentials(child) for child in item)
        return False

    if contains_credentials(value):
        raise ValueError("OAuth credentials cannot be stored in extra")
    normalized = normalize_scope_config(value)
    capabilities = normalized.get("capabilities")
    if capabilities is not None and (
        not isinstance(capabilities, list)
        or any(not isinstance(capability, str) for capability in capabilities)
    ):
        raise ValueError("extra.capabilities must be a list of strings")
    headers = normalized.get("headers")
    if headers is None:
        return normalized
    if not isinstance(headers, dict):
        raise ValueError("extra.headers must be an object")
    if any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(header_value, str)
        or not header_value.strip()
        for name, header_value in headers.items()
    ):
        raise ValueError("extra.headers must map non-empty names to string values")
    normalized["headers"] = {
        name.strip(): header_value for name, header_value in headers.items()
    }
    return normalized


class ChannelBase(BaseModel):
    """Base channel schema."""
    name: str = Field(..., min_length=1, max_length=100, description="Channel name")
    type: str = Field(..., min_length=1, max_length=50, description="Provider type")
    auth_type: Literal["api_key", "oauth"] = "api_key"
    base_url: str = Field(..., min_length=1, max_length=500, description="Base URL")
    models: List[str] = Field(default_factory=list, description="Available models")
    model_mapping: Dict[str, str] = Field(default_factory=dict, description="Model name mapping")
    priority: int = Field(default=1, ge=0, description="Channel priority")
    weight: int = Field(default=1, ge=1, description="Channel weight")
    enabled: bool = Field(default=True, description="Enable status")
    test_only: bool = Field(default=False, description="Test only flag")
    protocol: str = Field(default="openai", description="Protocol type")
    rpm_limit: Optional[int] = Field(None, description="RPM limit")
    tpm_limit: Optional[int] = Field(None, description="TPM limit")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Extra configuration")


class ChannelCreate(ChannelBase):
    """Schema for creating a channel."""
    key: Optional[str] = Field(None, min_length=1, max_length=500, description="API Key")
    oauth_session_id: Optional[str] = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_api_key(self):
        if self.auth_type == "api_key" and not self.key:
            raise ValueError("An API key is required")
        return self

    @field_validator("extra")
    @classmethod
    def validate_resource_scopes(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _normalize_extra(value)


class ChannelUpdate(BaseModel):
    """Schema for updating a channel."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    type: Optional[str] = Field(None, min_length=1, max_length=50)
    auth_type: Optional[Literal["api_key", "oauth"]] = None
    oauth_session_id: Optional[str] = Field(None, min_length=1, max_length=64)
    key: Optional[str] = Field(None, min_length=1, max_length=500)
    base_url: Optional[str] = Field(None, min_length=1, max_length=500)
    models: Optional[List[str]] = None
    model_mapping: Optional[Dict[str, str]] = None
    priority: Optional[int] = Field(None, ge=0)
    weight: Optional[int] = Field(None, ge=1)
    enabled: Optional[bool] = None
    test_only: Optional[bool] = None
    protocol: Optional[str] = None
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    extra: Optional[Dict[str, Any]] = None

    @field_validator("auth_type", "key")
    @classmethod
    def reject_null_authentication(cls, value):
        if value is None:
            raise ValueError("Authentication fields cannot be null")
        return value

    @field_validator("extra")
    @classmethod
    def validate_resource_scopes(
        cls,
        value: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        return _normalize_extra(value) if value is not None else None


class ChannelResponse(ChannelBase):
    """Schema for channel response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    auth_status: str = "api_key"
    auth_expires_at: Optional[float] = None
    auth_error: Optional[str] = None
    total_requests: int
    success_requests: int
    failed_requests: int
    created_at: datetime
    updated_at: datetime


class ChannelListItem(BaseModel):
    """Schema for channel list items."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    auth_type: Literal["api_key", "oauth"] = "api_key"
    auth_status: str = "api_key"
    auth_expires_at: Optional[float] = None
    auth_error: Optional[str] = None
    base_url: str
    enabled: bool
    priority: int
    weight: int
    models: List[str]
    protocol: str
    extra: Dict[str, Any]
    total_requests: int
    success_requests: int
    failed_requests: int


class ChannelUsageWindow(BaseModel):
    """One ChatGPT plan usage window (5-hour primary or weekly secondary)."""

    used_percent: Optional[float] = None
    window_seconds: Optional[int] = None
    reset_after_seconds: Optional[int] = None
    reset_at: Optional[float] = None


class ChannelUsageResponse(BaseModel):
    """ChatGPT plan usage snapshot for an OAuth channel."""

    plan: Optional[str] = None
    allowed: Optional[bool] = None
    limit_reached: Optional[bool] = None
    primary: Optional[ChannelUsageWindow] = None
    secondary: Optional[ChannelUsageWindow] = None
    fetched_at: float


class ChannelCooldownState(BaseModel):
    """Process-local routing cooldown for one `(model, channel)` pair."""

    model: str
    channel_id: int
    channel_name: Optional[str] = None
    phase: str = Field(description="cooldown or probe")
    remaining_seconds: int = Field(ge=0)
    cooldown_seconds: float = Field(ge=0)
    known_channel: bool = True


class ChannelRoutingStateResponse(BaseModel):
    """Snapshot of in-memory recovery state, not persisted configuration."""

    cooldowns: List[ChannelCooldownState]


class ChannelCooldownResetRequest(BaseModel):
    """Operator override: clear cooldowns for a channel.

    Omitting `model` clears every model on the channel; otherwise only that
    `(model, channel)` pair is released.
    """

    model: Optional[str] = Field(default=None, min_length=1)


class ChannelCooldownResetResponse(BaseModel):
    """Models whose process-local cooldown was cleared."""

    channel_id: int
    released: List[str]
