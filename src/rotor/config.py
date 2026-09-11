from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from pydantic_settings import BaseSettings

from rotor import __version__ as ROTOR_VERSION


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rotor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # API Settings
    API_V1_STR: str = "/v1"
    PROJECT_NAME: str = "Rotor"
    VERSION: str = ROTOR_VERSION

    API_KEY_PREFIX: str = "sk-"
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
    # MindAgent starts the independent Rotor MCP Server only when an explicit
    # command is configured. The Control token is passed through the child
    # process environment, never exposed as a tool argument.
    ROTOR_MINDAGENT_MCP_COMMAND: str | None = None
    ROTOR_MINDAGENT_MCP_ARGS: list[str] = Field(default_factory=list)
    ROTOR_MINDAGENT_MCP_CWD: str | None = None
    # Database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DEFAULT_CACHE_DIR / 'rotor.db'}"
    # For PostgreSQL use:
    # DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost/rotor"

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
    CONVERSATION_STORE_ENABLED: bool = True
    CONVERSATION_STORE_DIR: str = str(DEFAULT_CACHE_DIR / "conversations")
    SAVE_CONVERSATION_BODY: bool = True
    SAVE_PROVIDER_RESPONSE: bool = True
    CONVERSATION_QUEUE_MAXSIZE: int = 10000


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
