import json
import logging
from pathlib import Path
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


logger = logging.getLogger(__name__)
DEFAULT_SETTINGS_PATH = Path.home() / ".rotor" / "settings.json"


class RoutingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "priority_weighted", "fallback_order", "weighted", "adaptive"
    ] = (
        "priority_weighted"
    )
    affinity_enabled: bool = True
    adaptive_success_weight: float = Field(default=0.55, ge=0)
    adaptive_latency_weight: float = Field(default=0.25, ge=0)
    adaptive_cost_weight: float = Field(default=0.10, ge=0)
    adaptive_load_weight: float = Field(default=0.10, ge=0)
    adaptive_ewma_alpha: float = Field(default=0.20, gt=0, le=1)
    adaptive_prior_successes: float = Field(default=9.0, ge=0)
    adaptive_prior_failures: float = Field(default=1.0, ge=0)
    adaptive_latency_target_ms: float = Field(default=2_000.0, gt=0)
    adaptive_cost_target: float = Field(default=0.01, gt=0)


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    display_timezone: str = "Asia/Shanghai"

    @field_validator("display_timezone")
    @classmethod
    def validate_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("display_timezone must be a valid IANA timezone") from exc
        return value


class ApplicationSettingsStore:
    """Load and persist settings that can be changed while Rotor is running."""

    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH):
        self.path = path.expanduser()
        self._lock = RLock()
        self._settings = self._load()

    def _load(self) -> ApplicationSettings:
        if not self.path.exists():
            return ApplicationSettings()
        try:
            return ApplicationSettings.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            logger.warning("Could not load application settings from %s: %s", self.path, exc)
            return ApplicationSettings()

    def get(self) -> ApplicationSettings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def save(self, settings: ApplicationSettings) -> ApplicationSettings:
        validated = ApplicationSettings.model_validate(settings)
        payload = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(".json.tmp")
            temporary_path.write_text(f"{payload}\n", encoding="utf-8")
            temporary_path.replace(self.path)
            self._settings = validated
            return self._settings.model_copy(deep=True)


application_settings = ApplicationSettingsStore()
