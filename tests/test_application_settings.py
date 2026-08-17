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
    assert settings.display_timezone == "Asia/Shanghai"


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

    payload = json.loads(path.read_text())
    assert payload["routing"]["strategy"] == "fallback_order"
    assert payload["routing"]["affinity_enabled"] is False
    assert payload["routing"]["adaptive_success_weight"] == 0.55
    reloaded = ApplicationSettingsStore(path).get()
    assert reloaded.routing.strategy == "fallback_order"
    assert reloaded.routing.affinity_enabled is False


def test_settings_store_persists_display_timezone(tmp_path) -> None:
    path = tmp_path / ".rotor" / "settings.json"
    store = ApplicationSettingsStore(path)

    store.save(ApplicationSettings(display_timezone="UTC"))

    assert ApplicationSettingsStore(path).get().display_timezone == "UTC"


def test_settings_reject_unknown_routing_strategy() -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate(
            {"routing": {"strategy": "last_channel"}}
        )


def test_settings_accept_adaptive_routing_strategy() -> None:
    settings = ApplicationSettings.model_validate({
        "routing": {
            "strategy": "adaptive",
            "adaptive_ewma_alpha": 0.4,
        }
    })

    assert settings.routing.strategy == "adaptive"
    assert settings.routing.adaptive_ewma_alpha == 0.4


def test_settings_reject_invalid_display_timezone() -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings(display_timezone="not/a-timezone")
