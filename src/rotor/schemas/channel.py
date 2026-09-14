from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime

from rotor.core.resource_scopes import normalize_scope_config


def _normalize_extra(value: Dict[str, Any]) -> Dict[str, Any]:
    """Validate channel-level provider overrides while preserving extensions."""
    normalized = normalize_scope_config(value)
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
    key: str = Field(..., min_length=1, max_length=500, description="API Key")

    @field_validator("extra")
    @classmethod
    def validate_resource_scopes(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _normalize_extra(value)


class ChannelUpdate(BaseModel):
    """Schema for updating a channel."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    type: Optional[str] = Field(None, min_length=1, max_length=50)
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
