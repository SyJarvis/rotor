from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MCPControlKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MCPControlKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key_hint: str
    name: str
    scopes: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None


class MCPControlKeyCreated(MCPControlKeyResponse):
    key: str
    control_api_url: str
