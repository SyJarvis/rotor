import httpx

from rotor.core.exceptions import (
    UpstreamOverloaded,
    is_overload_error_signal,
    normalize_upstream_error,
)
from rotor.schemas.error import ErrorCategory, ErrorPhase


def _http_status_error(status_code: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    response = httpx.Response(
        status_code,
        request=request,
        headers={"x-request-id": "provider-request-1"},
        json=body,
    )
    return httpx.HTTPStatusError(
        f"upstream returned {status_code}",
        request=request,
        response=response,
    )


def test_normalize_upstream_error_classifies_and_sanitizes_bad_request() -> None:
    error = _http_status_error(
        400,
        {
            "error": {
                "message": "unsupported parameter",
                "api_key": "secret-value",
            }
        },
    )

    fact = normalize_upstream_error(error)

    assert fact.category == ErrorCategory.PROTOCOL_OR_PARAMETER_ERROR
    assert fact.phase == ErrorPhase.PROVIDER_REQUEST
    assert fact.upstream_status == 400
    assert fact.retryable is False
    assert fact.provider_request_ids == {"x-request-id": "provider-request-1"}
    assert fact.sanitized_body["error"]["api_key"] == "[redacted]"


def test_normalize_upstream_error_classifies_timeout_as_retryable() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/messages")

    fact = normalize_upstream_error(httpx.ReadTimeout("timed out", request=request))

    assert fact.category == ErrorCategory.TIMEOUT
    assert fact.code == "upstream_timeout"
    assert fact.upstream_status is None
    assert fact.retryable is True
    assert fact.retry_same_channel is True
    assert fact.fallback_allowed is True
    assert fact.sanitized_body is None


def test_normalize_upstream_error_classifies_rate_limit_as_retryable() -> None:
    error = _http_status_error(429, {"error": {"message": "rate limited"}})
    error.response.headers["retry-after"] = "2.5"

    fact = normalize_upstream_error(error)

    assert fact.category == ErrorCategory.QUOTA_OR_RATE_LIMIT
    assert fact.code == "upstream_rate_limited"
    assert fact.retryable is True
    assert fact.retry_same_channel is True
    assert fact.fallback_allowed is True
    assert fact.retry_after_seconds == 2.5


def test_normalize_upstream_error_does_not_retry_auth_on_same_channel() -> None:
    fact = normalize_upstream_error(
        _http_status_error(401, {"error": {"message": "invalid key"}})
    )

    assert fact.category == ErrorCategory.AUTHENTICATION_OR_PERMISSION
    assert fact.retryable is True
    assert fact.retry_same_channel is False
    assert fact.fallback_allowed is True


def test_normalize_upstream_error_rejects_fallback_for_bad_request() -> None:
    fact = normalize_upstream_error(
        _http_status_error(400, {"error": {"message": "bad parameter"}})
    )

    assert fact.retryable is False
    assert fact.retry_same_channel is False
    assert fact.fallback_allowed is False


def test_normalize_upstream_error_does_not_assume_every_404_is_missing_model() -> None:
    fact = normalize_upstream_error(
        _http_status_error(404, {"error": {"message": "route not found"}})
    )

    assert fact.code == "upstream_resource_not_found"
    assert fact.category == ErrorCategory.PROTOCOL_OR_PARAMETER_ERROR
    assert fact.fallback_allowed is True


def test_normalize_upstream_error_detects_explicit_missing_model() -> None:
    fact = normalize_upstream_error(
        _http_status_error(
            404,
            {"error": {"code": "model_not_found", "message": "model not found"}},
        )
    )

    assert fact.code == "upstream_model_not_found"
    assert fact.category == ErrorCategory.MODEL_NOT_FOUND


def test_normalize_upstream_error_classifies_stream_overload_as_fallbackable() -> None:
    fact = normalize_upstream_error(
        UpstreamOverloaded(
            "Our servers are currently overloaded. Please try again later.",
            error_type="overloaded_error",
        )
    )

    assert fact.code == "upstream_overloaded"
    assert fact.category == ErrorCategory.UPSTREAM_AVAILABILITY
    assert fact.fallback_allowed is True
    assert fact.retry_same_channel is False
    assert fact.retryable is True
    assert fact.message == (
        "Our servers are currently overloaded. Please try again later."
    )


def test_is_overload_error_signal_matches_explicit_types_and_prefixes() -> None:
    assert is_overload_error_signal("overloaded_error", None)
    assert is_overload_error_signal("Overloaded", None)
    assert is_overload_error_signal("rate_limit", None)
    assert is_overload_error_signal("rate_limit_exceeded", None)
    assert is_overload_error_signal("Too_Many_Requests", None)


def test_is_overload_error_signal_matches_message_wording() -> None:
    assert is_overload_error_signal(
        None, "Our servers are currently Overloaded. Please try again later."
    )
    assert is_overload_error_signal("api_error", "upstream is overloaded right now")


def test_is_overload_error_signal_rejects_parameter_and_auth_errors() -> None:
    assert not is_overload_error_signal("invalid_request_error", "max_tokens too large")
    assert not is_overload_error_signal("authentication_error", "invalid api key")
    assert not is_overload_error_signal(None, "internal server error")
    assert not is_overload_error_signal(None, None)
