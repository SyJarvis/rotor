from rotor.schemas.channel import ChannelCreate, ChannelResponse


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
