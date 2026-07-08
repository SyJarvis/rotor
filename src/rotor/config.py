from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rotor"


class Settings(BaseSettings):
    # API Settings
    API_V1_STR: str = "/v1"
    PROJECT_NAME: str = "Rotor"
    VERSION: str = "1.0.0"

    # Security
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    API_KEY_PREFIX: str = "sk-"
    # Database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DEFAULT_CACHE_DIR / 'rotor.db'}"
    # For PostgreSQL use:
    # DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost/rotor"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Request Settings
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 1.0
    REQUEST_TIMEOUT: float = 120.0

    # Default Channel Settings
    DEFAULT_CHANNEL_PRIORITY: int = 1
    DEFAULT_CHANNEL_WEIGHT: int = 1

    # Logging
    LOG_REQUESTS: bool = True
    LOG_LEVEL: str = "INFO"

    # Conversation storage
    CONVERSATION_STORE_ENABLED: bool = True
    CONVERSATION_STORE_DIR: str = str(DEFAULT_CACHE_DIR / "conversations")
    CONVERSATION_STORE_FORMAT: str = "jsonl"
    SAVE_CONVERSATION_BODY: bool = True
    SAVE_PROVIDER_RESPONSE: bool = True
    CONVERSATION_RETENTION_DAYS: int = 30

    # GLM Settings
    GLM_BASE_URL: str = ""
    GLM_API_KEY: str = ""
    GLM_MODEL: str = ""

    # MiniMax Settings
    MINIMAX_BASE_URL: str = ""
    MINIMAX_API_KEY: str = ""
    MINIMAX_MODEL: str = ""

    # Kimi Settings
    KIMI_BASE_URL: str = ""
    KIMI_API_KEY: str = ""
    KIMI_MODEL: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
