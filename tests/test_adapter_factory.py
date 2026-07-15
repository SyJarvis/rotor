from types import SimpleNamespace

import pytest

from rotor.adapters.factory import AdapterFactory


def test_bedrock_provider_is_not_registered_without_sigv4_support() -> None:
    channel = SimpleNamespace(type="bedrock")

    with pytest.raises(ValueError, match="Unsupported provider type: bedrock"):
        AdapterFactory.create_adapter(channel, http_client=None)

    assert "bedrock" not in AdapterFactory.get_supported_providers()
