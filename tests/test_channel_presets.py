import asyncio
from types import SimpleNamespace

import rotor.api.admin.channels as channels_api
from rotor.adapters.factory import AdapterFactory
from rotor.api.admin.channels import _fetch_model_list, _probe_generation_capability
from rotor.channels.presets import (
    channel_option,
    join_api_url,
    provider_defaults,
    provider_headers,
)


def test_join_api_url_appends_relative_path() -> None:
    assert (
        join_api_url("https://api.openai.com/v1/", "/chat/completions")
        == "https://api.openai.com/v1/chat/completions"
    )


def test_join_api_url_does_not_duplicate_base_path() -> None:
    assert (
        join_api_url("https://example.com/v1", "/v1/models")
        == "https://example.com/v1/models"
    )


def test_channel_option_prefers_explicit_extra() -> None:
    assert channel_option(
        provider="deepseek",
        protocol="openai",
        extra={"models_path": "/custom/models"},
        name="models_path",
    ) == "/custom/models"


def test_anthropic_defaults_and_headers() -> None:
    defaults = provider_defaults("anthropic", "anthropic")
    assert defaults["request_path"] == "/messages"
    headers = provider_headers(key="secret", auth_type=defaults["auth_type"])
    assert headers["x-api-key"] == "secret"
    assert "Authorization" not in headers


def test_provider_headers_allow_channel_specific_session_header() -> None:
    headers = provider_headers(
        key="secret",
        auth_type="bearer",
        extra_headers={"x-opencode-session": "coding-plan-session"},
    )

    assert headers["x-opencode-session"] == "coding-plan-session"


def test_channel_schema_validates_extra_headers() -> None:
    from pydantic import ValidationError
    from rotor.schemas.channel import ChannelCreate

    channel = ChannelCreate(
        name="coding-plan",
        type="openai",
        base_url="https://provider.example",
        key="secret",
        extra={"headers": {"x-opencode-session": "session-1"}},
    )
    assert channel.extra["headers"]["x-opencode-session"] == "session-1"

    try:
        ChannelCreate(
            name="invalid",
            type="openai",
            base_url="https://provider.example",
            key="secret",
            extra={"headers": {"x-opencode-session": 123}},
        )
    except ValidationError as exc:
        assert "extra.headers" in str(exc)
    else:
        raise AssertionError("non-string channel header value must be rejected")

    try:
        ChannelCreate(
            name="blank-header",
            type="openai",
            base_url="https://provider.example",
            key="secret",
            extra={"headers": {"x-opencode-session": "  "}},
        )
    except ValidationError as exc:
        assert "extra.headers" in str(exc)
    else:
        raise AssertionError("blank channel header value must be rejected")


def test_anthropic_protocol_keeps_non_anthropic_provider_auth() -> None:
    defaults = provider_defaults("zhipu", "anthropic")

    assert defaults["request_path"] == "/messages"
    assert defaults["models_path"] == "/models"
    assert defaults["auth_type"] == "bearer"


def test_anthropic_model_probe_keeps_provider_bearer_auth(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "glm-test"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(channels_api.httpx, "AsyncClient", FakeClient)

    models = asyncio.run(_fetch_model_list(
        base_url="https://example.test/v1",
        key="secret",
        provider_type="zhipu",
        protocol="anthropic",
    ))

    assert models == ["glm-test"]
    assert captured["url"] == "https://example.test/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert "x-api-key" not in captured["headers"]


def test_responses_protocol_uses_responses_request_path() -> None:
    defaults = provider_defaults("openai", "openai_responses")

    assert defaults["request_path"] == "/responses"
    assert defaults["models_path"] == "/models"
    assert defaults["auth_type"] == "bearer"


def test_bearer_headers() -> None:
    headers = provider_headers(key="secret", auth_type="bearer")
    assert headers["Authorization"] == "Bearer secret"


def test_function_call_capability_probe_uses_configured_adapter(monkeypatch) -> None:
    captured = {}

    class FakeAdapter:
        async def make_request(self, request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return object()

        async def convert_response(self, response, request):
            return {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "rotor_health_check",
                                "arguments": "{}",
                            },
                        }],
                    },
                }],
            }

    monkeypatch.setattr(
        AdapterFactory,
        "create_adapter",
        lambda channel, client: FakeAdapter(),
    )
    channel = SimpleNamespace(protocol="openai_responses")

    asyncio.run(_probe_generation_capability(
        channel=channel,
        model="test-model",
        capability="function_call",
    ))

    assert captured["request"].tools[0].function.name == "rotor_health_check"
    assert captured["request"].tool_choice["function"]["name"] == "rotor_health_check"
    assert captured["timeout"] == 30.0
