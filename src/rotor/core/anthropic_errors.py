"""Anthropic error envelopes without exposing validation inputs or credentials."""

import uuid

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import HTTPStatusError
from starlette.exceptions import HTTPException

from rotor.core.exceptions import (
    UpstreamOverloaded, UpstreamProtocolError,
    classify_error_status, general_exception_handler,
)


def is_anthropic_request(request) -> bool:
    path = request.url.path.rstrip("/")
    return path.startswith("/anthropic/") or path in {"/v1/messages", "/v1/messages/count_tokens"}


def anthropic_error_payload(exc, *, request_id=None, secret=None):
    status_code = exc.status_code if isinstance(exc, HTTPException) else classify_error_status(exc)
    error_type = {
        400: "invalid_request_error", 401: "authentication_error", 402: "billing_error", 403: "permission_error",
        404: "not_found_error", 413: "request_too_large", 422: "invalid_request_error",
        409: "conflict_error", 429: "rate_limit_error", 503: "overloaded_error",
        504: "timeout_error", 529: "overloaded_error",
    }.get(status_code, "api_error")
    message = "Internal server error"
    if isinstance(exc, UpstreamProtocolError):
        error_type = exc.error_type
        message = str(exc)
    elif isinstance(exc, UpstreamOverloaded):
        error_type = "rate_limit_error" if str(exc.error_type or "").startswith("rate_limit") else "overloaded_error"
        message = "Upstream is temporarily unavailable"
    elif isinstance(exc, HTTPStatusError):
        message = f"Upstream returned HTTP {exc.response.status_code}"
    elif isinstance(exc, RequestValidationError):
        error_type, message = "invalid_request_error", "Invalid request parameters"
    elif isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            error = detail.get("error")
            detail = error.get("message", "Request failed") if isinstance(error, dict) else "Request failed"
        message = str(detail) if isinstance(detail, str) else "Request failed"
    elif status_code == 504:
        message = "Upstream request timed out"
    elif status_code == 502:
        message = "Upstream request failed"
    if secret:
        message = message.replace(secret, "[redacted]")
    return {"type": "error", "error": {"type": error_type, "message": message},
            "request_id": request_id or f"req_{uuid.uuid4().hex}"}


def anthropic_error_response(request, exc):
    if isinstance(exc, RequestValidationError):
        status_code = 422
    elif isinstance(exc, HTTPException):
        status_code = exc.status_code
    elif isinstance(exc, UpstreamOverloaded):
        status_code = 529
    else:
        status_code = classify_error_status(exc)
    headers = dict(getattr(exc, "headers", None) or {})
    if isinstance(exc, HTTPStatusError) and exc.response.headers.get("retry-after"):
        headers["Retry-After"] = exc.response.headers["retry-after"]
    request_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id")
    return JSONResponse(status_code=status_code, headers=headers,
                        content=anthropic_error_payload(exc, request_id=request_id))


async def protocol_http_exception_handler(request, exc):
    if is_anthropic_request(request):
        return anthropic_error_response(request, exc)
    return await http_exception_handler(request, exc)


async def protocol_validation_exception_handler(request, exc):
    if is_anthropic_request(request):
        return anthropic_error_response(request, exc)
    return await request_validation_exception_handler(request, exc)


async def protocol_upstream_exception_handler(request, exc):
    if is_anthropic_request(request):
        return anthropic_error_response(request, exc)
    return await general_exception_handler(request, exc)
