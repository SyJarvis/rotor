import json
import logging
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger(__name__)
DEFAULT_SETTINGS_PATH = Path.home() / ".rotor" / "settings.json"


class RoutingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["priority_weighted", "fallback_order", "weighted"] = (
        "priority_weighted"
    )
    affinity_enabled: bool = True


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing: RoutingSettings = Field(default_factory=RoutingSettings)


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
