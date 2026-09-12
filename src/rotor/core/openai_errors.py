"""OpenAI-protocol error envelope for gateway and upstream failures.

Anthropic-protocol clients receive a structured envelope from
``anthropic_errors``.  OpenAI-protocol clients used to fall through to the
generic 500 handler, which dropped the upstream status and body that a
rejected request needs for diagnosis.
"""

from typing import Any

from fastapi.responses import JSONResponse
from httpx import HTTPStatusError
from starlette.exceptions import HTTPException

from rotor.core.exceptions import (
    UpstreamOverloaded,
    classify_error_status,
    normalize_upstream_error,
)

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    503: "overloaded_error",
    504: "timeout_error",
}


def _status_code_for(exc: Exception) -> int:
    if isinstance(exc, HTTPException):
        return exc.status_code
    if isinstance(exc, UpstreamOverloaded):
        # Anthropic answers 529; OpenAI-compatible clients expect 503.
        return 503
    return classify_error_status(exc)


def openai_error_payload(exc: Exception) -> dict[str, Any]:
    """Return an OpenAI-compatible error body that keeps upstream diagnostics.

    The upstream status, request ids and sanitized response body come from the
    same normalization used for request accounting, so what a client sees here
    matches what the request log stores.
    """
    fact = normalize_upstream_error(exc)
    status_code = _status_code_for(exc)
    error: dict[str, Any] = {
        "message": fact.message,
        "type": _ERROR_TYPES.get(status_code, "api_error"),
        "code": fact.code,
    }
    upstream: dict[str, Any] = {}
    if fact.upstream_status is not None:
        upstream["status_code"] = fact.upstream_status
    if fact.sanitized_body is not None:
        upstream["body"] = fact.sanitized_body
    if fact.provider_request_ids:
        upstream["request_ids"] = fact.provider_request_ids
    if upstream:
        error["upstream"] = upstream
    return {"error": error}


def openai_error_response(exc: Exception) -> JSONResponse:
    """Build the OpenAI-protocol response for a gateway failure."""
    headers: dict[str, str] = {}
    if isinstance(exc, HTTPStatusError) and exc.response.headers.get("retry-after"):
        headers["Retry-After"] = exc.response.headers["retry-after"]
    return JSONResponse(
        status_code=_status_code_for(exc),
        content=openai_error_payload(exc),
        headers=headers or None,
    )
