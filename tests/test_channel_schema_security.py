import pytest
from pydantic import ValidationError

from rotor.schemas.channel import ChannelCreate, ChannelResponse, ChannelUpdate


def _channel_payload() -> dict:
    return {
        "id": 1,
        "name": "provider-a",
        "type": "openai",
        "key": "secret-provider-key",
        "base_url": "https://api.example.com",
        "models": ["model-a"],
        "model_mapping": {},
        "priority": 1,
        "weight": 1,
        "enabled": True,
        "test_only": False,
        "protocol": "openai",
        "rpm_limit": None,
        "tpm_limit": None,
        "extra": {},
        "total_requests": 0,
        "success_requests": 0,
        "failed_requests": 0,
        "created_at": "2026-07-29T00:00:00Z",
        "updated_at": "2026-07-29T00:00:00Z",
    }


def test_channel_response_does_not_serialize_provider_key() -> None:
    response = ChannelResponse.model_validate(_channel_payload())

    assert "key" not in response.model_dump()
    assert "key" not in ChannelResponse.model_json_schema()["properties"]


def test_channel_create_still_requires_provider_key() -> None:
    payload = _channel_payload()

    channel = ChannelCreate.model_validate(payload)

    assert channel.key == "secret-provider-key"


@pytest.mark.parametrize("schema", [ChannelCreate, ChannelUpdate])
@pytest.mark.parametrize("capabilities", [123, "stream", {}, ["stream", 123], [None], [{}], [True]])
@pytest.mark.parametrize("headers", [None, {"X-Provider-Account": "account-1"}])
def test_channel_capabilities_reject_invalid_types(schema, capabilities, headers) -> None:
    extra = {"capabilities": capabilities}
    if headers is not None:
        extra["headers"] = headers
    payload = {**_channel_payload(), "extra": extra}

    with pytest.raises(ValidationError, match="extra.capabilities must be a list of strings"):
        schema.model_validate(payload)


@pytest.mark.parametrize("schema", [ChannelCreate, ChannelUpdate])
@pytest.mark.parametrize("capabilities", [None, [], ["stream", "function_call", "future-capability"]])
def test_channel_capabilities_preserve_valid_values_and_extensions(schema, capabilities) -> None:
    extra = {
        "capabilities": capabilities,
        "provider_extension": {"mode": "custom"},
        "headers": {"X-Provider-Account": "account-1"},
    }

    channel = schema.model_validate({**_channel_payload(), "extra": extra})

    assert channel.extra == extra


@pytest.mark.parametrize("schema", [ChannelCreate, ChannelUpdate])
def test_channel_extra_without_capabilities_remains_unchanged(schema) -> None:
    extra = {"provider_extension": {"mode": "custom"}}

    channel = schema.model_validate({**_channel_payload(), "extra": extra})

    assert channel.extra == extra
