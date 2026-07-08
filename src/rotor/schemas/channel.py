from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime


class ChannelBase(BaseModel):
    """Base channel schema."""
    name: str = Field(..., description="Channel name")
    type: str = Field(..., description="Provider type")
    key: str = Field(..., description="API Key")
    base_url: str = Field(..., description="Base URL")
    models: List[str] = Field(default_factory=list, description="Available models")
    model_mapping: Dict[str, str] = Field(default_factory=dict, description="Model name mapping")
    priority: int = Field(default=1, description="Channel priority")
    weight: int = Field(default=1, description="Channel weight")
    enabled: bool = Field(default=True, description="Enable status")
    test_only: bool = Field(default=False, description="Test only flag")
    protocol: str = Field(default="openai", description="Protocol type")
    rpm_limit: Optional[int] = Field(None, description="RPM limit")
    tpm_limit: Optional[int] = Field(None, description="TPM limit")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Extra configuration")


class ChannelCreate(ChannelBase):
    """Schema for creating a channel."""
    pass


class ChannelUpdate(BaseModel):
    """Schema for updating a channel."""
    name: Optional[str] = None
    type: Optional[str] = None
    key: Optional[str] = None
    base_url: Optional[str] = None
    models: Optional[List[str]] = None
    model_mapping: Optional[Dict[str, str]] = None
    priority: Optional[int] = None
    weight: Optional[int] = None
    enabled: Optional[bool] = None
    test_only: Optional[bool] = None
    protocol: Optional[str] = None
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    extra: Optional[Dict[str, Any]] = None


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
    total_requests: int
    success_requests: int
    failed_requests: int
