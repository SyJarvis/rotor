from types import SimpleNamespace

import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.anthropic import AnthropicAdapter


def test_bedrock_provider_is_not_registered_without_sigv4_support() -> None:
    channel = SimpleNamespace(type="bedrock")

    with pytest.raises(ValueError, match="Unsupported provider type: bedrock"):
        AdapterFactory.create_adapter(channel, http_client=None)

    assert "bedrock" not in AdapterFactory.get_supported_providers()


def test_explicit_anthropic_protocol_uses_anthropic_adapter() -> None:
    channel = SimpleNamespace(
        type="zhipu",
        protocol="anthropic",
        key="secret",
        extra={},
    )

    adapter = AdapterFactory.create_adapter(channel, http_client=None)

    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.build_request_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret",
    }
