"""OpenAI-protocol error envelope for gateway and upstream failures.

Anthropic-protocol clients receive a structured envelope from
``anthropic_errors``.  OpenAI-protocol clients used to fall through to the
generic 500 handler, which dropped the upstream status and body that a
rejected request needs for diagnosis.
"""

from typing import Any

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from rotor.core.exceptions import (
    ERROR_TYPES,
    classify_error_status,
    normalize_upstream_error,
    upstream_retry_after,
)


def _status_code_for(exc: Exception) -> int:
    if isinstance(exc, HTTPException):
        return exc.status_code
    # UpstreamOverloaded maps to 503 here (Anthropic envelopes use 529).
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
        "type": ERROR_TYPES.get(status_code, "api_error"),
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
    if retry_after := upstream_retry_after(exc):
        headers["Retry-After"] = retry_after
    return JSONResponse(
        status_code=_status_code_for(exc),
        content=openai_error_payload(exc),
        headers=headers or None,
    )
