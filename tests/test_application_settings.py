import json

import pytest
from pydantic import ValidationError

from rotor.application_settings import (
    ApplicationSettings,
    ApplicationSettingsStore,
    RoutingSettings,
)


def test_settings_store_uses_defaults_when_file_does_not_exist(tmp_path) -> None:
    store = ApplicationSettingsStore(tmp_path / "settings.json")

    settings = store.get()

    assert settings.routing.strategy == "priority_weighted"
    assert settings.routing.affinity_enabled is True


def test_settings_store_persists_and_reloads_routing_settings(tmp_path) -> None:
    path = tmp_path / ".rotor" / "settings.json"
    store = ApplicationSettingsStore(path)

    store.save(
        ApplicationSettings(
            routing=RoutingSettings(
                strategy="fallback_order",
                affinity_enabled=False,
            )
        )
    )

    assert json.loads(path.read_text()) == {
        "routing": {
            "strategy": "fallback_order",
            "affinity_enabled": False,
        }
    }
    reloaded = ApplicationSettingsStore(path).get()
    assert reloaded.routing.strategy == "fallback_order"
    assert reloaded.routing.affinity_enabled is False


def test_settings_reject_unknown_routing_strategy() -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate(
            {"routing": {"strategy": "last_channel"}}
        )
