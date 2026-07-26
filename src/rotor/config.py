from functools import lru_cache
from pathlib import Path

from pydantic_settings import SettingsConfigDict
from pydantic_settings import BaseSettings


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rotor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # API Settings
    API_V1_STR: str = "/v1"
    PROJECT_NAME: str = "Rotor"
    VERSION: str = "1.0.0"

    API_KEY_PREFIX: str = "sk-"
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
