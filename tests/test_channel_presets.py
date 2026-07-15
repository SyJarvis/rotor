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


def test_bearer_headers() -> None:
    headers = provider_headers(key="secret", auth_type="bearer")
    assert headers["Authorization"] == "Bearer secret"
