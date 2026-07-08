from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class TokenBase(BaseModel):
    """Base token schema."""
    key: str = Field(..., description="API token key", min_length=10)
    name: str = Field(..., description="Token name")
    user_id: Optional[str] = Field(None, description="User ID")
    quota: Optional[int] = Field(None, description="Token quota (null = unlimited)")
    group: str = Field(default="default", description="User group")
    allowed_channels: Optional[List[int]] = Field(None, description="Allowed channel IDs")
    enabled: bool = Field(default=True, description="Enable status")
    expire_time: Optional[datetime] = Field(None, description="Expiration time")


class TokenCreate(TokenBase):
    """Schema for creating a token."""
    pass


class TokenUpdate(BaseModel):
    """Schema for updating a token."""
    key: Optional[str] = None
    name: Optional[str] = None
    user_id: Optional[str] = None
    quota: Optional[int] = None
    group: Optional[str] = None
    allowed_channels: Optional[List[int]] = None
    enabled: Optional[bool] = None
    expired: Optional[bool] = None
    expire_time: Optional[datetime] = None


class TokenResponse(TokenBase):
    """Schema for token response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    used_quota: int
    expired: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime]
    request_count: int
    token_count: int


class TokenListItem(BaseModel):
    """Schema for token list items."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    name: str
    user_id: Optional[str]
    enabled: bool
    expired: bool
    quota: Optional[int]
    used_quota: int
    group: str
    created_at: datetime
