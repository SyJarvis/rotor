from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rotor.core.resource_scopes import (
    normalize_scope_config,
    resolve_resource_scopes,
)
from rotor.schemas.channel import ChannelCreate, ChannelUpdate


def _channel(channel_id: int, extra: object) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id, extra=extra)


def test_missing_scopes_default_to_independent_channel_boundaries() -> None:
    first = resolve_resource_scopes(_channel(1, {}))
    second = resolve_resource_scopes(_channel(2, {}))

    assert first.as_dict() == {
        "cache_scope": "channel:1",
        "capacity_scope": "channel:1",
        "billing_scope": "channel:1",
    }
    assert second.capacity_scope == "channel:2"


def test_matching_explicit_scope_ids_group_different_channels() -> None:
    first = resolve_resource_scopes(_channel(1, {
        "cache_scope": "provider/account-a/cache",
        "capacity_scope": "provider/account-a/capacity",
        "billing_scope": "provider/account-a/billing",
    }))
    second = resolve_resource_scopes(_channel(2, {
        "cache_scope": "provider/account-a/cache",
        "capacity_scope": "provider/account-a/capacity",
        "billing_scope": "provider/account-a/billing",
    }))

    assert first == second


def test_invalid_persisted_scope_falls_back_to_channel_isolation() -> None:
    scopes = resolve_resource_scopes(_channel(7, {
        "cache_scope": [],
        "capacity_scope": "contains spaces",
        "billing_scope": "",
    }))

    assert scopes.as_dict() == {
        "cache_scope": "channel:7",
        "capacity_scope": "channel:7",
        "billing_scope": "channel:7",
    }


def test_scope_config_normalizes_values_and_preserves_other_extra() -> None:
    normalized = normalize_scope_config({
        "capacity_scope": "  account-a  ",
        "billing_scope": None,
        "headers": {"X-Test": "value"},
    })

    assert normalized == {
        "capacity_scope": "account-a",
        "headers": {"X-Test": "value"},
    }


@pytest.mark.parametrize(
    "schema",
    [ChannelCreate, ChannelUpdate],
)
@pytest.mark.parametrize(
    "scope",
    ["", "contains spaces", [], "a" * 101],
)
def test_channel_schemas_reject_invalid_scope_ids(schema, scope) -> None:
    payload = {"extra": {"capacity_scope": scope}}
    if schema is ChannelCreate:
        payload.update({
            "name": "channel-a",
            "type": "openai",
            "key": "provider-key",
            "base_url": "https://api.example.com/v1",
        })

    with pytest.raises(ValidationError):
        schema.model_validate(payload)
