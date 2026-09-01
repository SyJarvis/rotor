from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class AdminPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class AdminActorResponse(BaseModel):
    authenticated: bool = True
    actor_id: str
    username: str
    auth_method: str
    must_change_password: bool


class AdminAuthEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admin_user_id: int | None
    username: str
    event_type: str
    success: bool
    reason: str | None
    client_ip: str
    user_agent: str | None
    created_at: datetime
