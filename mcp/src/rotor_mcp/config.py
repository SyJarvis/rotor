from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ROTOR_",
        extra="ignore",
    )

    control_api_url: str = "http://127.0.0.1:8000/api/control/v1"
    control_api_token: SecretStr | None = None
    agent_id: str = "mindagent"
    agent_run_id: str | None = None
    control_api_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    control_api_max_response_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024,
    )

    @field_validator("control_api_url")
    @classmethod
    def normalize_control_api_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("must use http:// or https://")
        return normalized

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        if len(normalized) > 128 or "\r" in normalized or "\n" in normalized:
            raise ValueError("must be a valid HTTP header value")
        return normalized

    @field_validator("agent_run_id")
    @classmethod
    def validate_agent_run_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 128
            or "\r" in normalized
            or "\n" in normalized
        ):
            raise ValueError("must be a valid HTTP header value")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
