from types import SimpleNamespace

from rotor.gateway.provider_facts import (
    extract_capacity_snapshot,
    extract_capacity_snapshot_from_error,
)


def test_capacity_snapshot_keeps_only_rate_limit_headers() -> None:
    snapshot = extract_capacity_snapshot({
        "Authorization": "Bearer secret",
        "X-Request-Id": "provider-request",
        "X-RateLimit-Limit-Requests": "500",
        "x-ratelimit-remaining-requests": "499",
        "anthropic-ratelimit-input-tokens-reset": "2026-08-19T01:00:00Z",
        "Retry-After": "2",
    })

    assert snapshot == {
        "anthropic-ratelimit-input-tokens-reset": "2026-08-19T01:00:00Z",
        "retry-after": "2",
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "499",
    }


def test_capacity_snapshot_returns_none_without_capacity_headers() -> None:
    assert extract_capacity_snapshot({"X-Request-Id": "request-1"}) is None


def test_capacity_snapshot_can_be_extracted_from_http_error() -> None:
    error = SimpleNamespace(response=SimpleNamespace(headers={
        "Retry-After": "3",
        "X-RateLimit-Remaining-Tokens": "0",
    }))

    assert extract_capacity_snapshot_from_error(error) == {
        "retry-after": "3",
        "x-ratelimit-remaining-tokens": "0",
    }
