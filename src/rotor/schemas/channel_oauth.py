from typing import Literal

from pydantic import BaseModel, Field


class OAuthSessionCreate(BaseModel):
    channel_id: int | None = Field(None, ge=1)


class OAuthSessionResponse(BaseModel):
    session_id: str
    status: Literal["pending", "exchanging", "authorized", "failed", "expired", "cancelled", "consumed"]
    authorization_url: str | None
    redirect_uri: Literal["http://localhost:1455/auth/callback"]
    expires_at: float
    error: str | None = None


class OAuthCallbackRequest(BaseModel):
    callback_url: str = Field(..., min_length=1, max_length=8192)


class OAuthCallbackResponse(BaseModel):
    code: str
    state: str
    redirect_uri: Literal["http://localhost:1455/auth/callback"]
