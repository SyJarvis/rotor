from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import SettingsConfigDict
from pydantic_settings import BaseSettings

from rotor import __version__ as ROTOR_VERSION


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rotor"


def oauth_redirect_parts(uri: str):
    """Validate a registered redirect without trusting URL normalization."""
    if not isinstance(uri, str) or len(uri) > 2048 or any(
        ord(character) <= 32 or ord(character) == 127
        for character in unquote(uri)
    ) or "\\" in unquote(uri):
        raise ValueError("OAuth redirect URI contains invalid characters")
    parsed = urlsplit(uri)
    if (parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query or "?" in uri or "#" in uri
            or not parsed.hostname or not parsed.path.startswith("/")):
        raise ValueError("OAuth redirect URI must have a host and path, without credentials, query or fragment")
    _ = parsed.port
    if parsed.scheme == "https":
        return parsed
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}:
        return parsed
    raise ValueError("OAuth redirect URI must use HTTPS or a loopback IP")


class OAuthClientSettings(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_redirects(self):
        for uri in self.redirect_uris:
            oauth_redirect_parts(uri)
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    @model_validator(mode="before")
    @classmethod
    def ignore_retired_chat_mcp_settings(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        retired = {
            "ROTOR_MINDAGENT_MCP_COMMAND",
            "ROTOR_MINDAGENT_MCP_ARGS",
            "ROTOR_MINDAGENT_MCP_CWD",
        }
        return {key: value for key, value in values.items() if key not in retired}

    # API Settings
    API_V1_STR: str = "/v1"
    PROJECT_NAME: str = "Rotor"
    VERSION: str = ROTOR_VERSION

    API_KEY_PREFIX: str = "sk-"
    ROTOR_OAUTH_CLIENTS: dict[str, OAuthClientSettings] = Field(default_factory=lambda: {
        "rotor-client": OAuthClientSettings(name="Rotor Client", redirect_uris=[
            "http://127.0.0.1/auth/callback", "http://[::1]/auth/callback",
        ]),
    })
    ROTOR_OAUTH_ISSUER: str | None = None

    @model_validator(mode="after")
    def validate_oauth_settings(self):
        for client_id in self.ROTOR_OAUTH_CLIENTS:
            if not client_id or len(client_id) > 100 or any(ord(char) <= 32 or ord(char) == 127 for char in client_id):
                raise ValueError("OAuth client IDs must contain 1–100 non-space characters")
        if self.ROTOR_OAUTH_ISSUER is not None:
            parsed = oauth_redirect_parts(self.ROTOR_OAUTH_ISSUER.rstrip("/") + "/")
            if parsed.scheme != "https":
                raise ValueError("ROTOR_OAUTH_ISSUER must use HTTPS")
            self.ROTOR_OAUTH_ISSUER = self.ROTOR_OAUTH_ISSUER.rstrip("/")
        return self
    ROTOR_DEFAULT_ADMIN_USERNAME: str = "admin"
    ROTOR_DEFAULT_ADMIN_PASSWORD: str = "123456"
    ROTOR_ADMIN_SESSION_IDLE_SECONDS: int = 1800
    ROTOR_ADMIN_SESSION_TTL_SECONDS: int = 43200
    ROTOR_ADMIN_COOKIE_SECURE: bool = False
    ROTOR_ADMIN_LOGIN_MAX_FAILURES: int = Field(default=5, ge=1)
    ROTOR_ADMIN_LOGIN_WINDOW_SECONDS: int = Field(default=300, ge=1)
    ROTOR_ADMIN_LOGIN_LOCK_SECONDS: int = Field(default=900, ge=1)
    # A single Bearer token keeps the first Control API/MCP integration
    # intentionally simple. It remains separate from ordinary Rotor API keys.
    ROTOR_CONTROL_API_TOKEN: str | None = None
    ROTOR_CONTROL_API_SCOPES: list[str] = Field(
        default_factory=lambda: [
            "channel:read",
            "request_trace:read",
            "usage:read",
        ]
    )
    ROTOR_CONTROL_ACTOR_ID: str = "rotor-agent"
    ROTOR_CONTROL_CLIENT_ID: str = "rotor-mcp"
    ROTOR_CONTROL_API_URL: str = "http://127.0.0.1:8000/api/control/v1"
    # Database. This release runs the startup migration path against a
    # file-backed SQLite database only; other drivers are rejected at startup.
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DEFAULT_CACHE_DIR / 'rotor.db'}"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Request Settings
    REQUEST_TIMEOUT: float = 120.0
    CONNECT_TIMEOUT: float = 10.0
    WRITE_TIMEOUT: float = 30.0
    POOL_TIMEOUT: float = 10.0

    # Logging
    LOG_LEVEL: str = "INFO"
    ROTOR_LOG_DIR: str = str(DEFAULT_CACHE_DIR / "logs")

    # Conversation storage
    CHAT_STORE_DIR: str = str(DEFAULT_CACHE_DIR / "chats")

    CONVERSATION_STORE_ENABLED: bool = True
    CONVERSATION_STORE_DIR: str = str(DEFAULT_CACHE_DIR / "conversations")
    SAVE_CONVERSATION_BODY: bool = True
    SAVE_PROVIDER_RESPONSE: bool = True
    CONVERSATION_QUEUE_MAXSIZE: int = 10000


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
