import json
import logging
import math
from datetime import timezone
from email.utils import parsedate_to_datetime
from time import time
from typing import Any, Optional
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from rotor.schemas.error import ErrorCategory, ErrorPhase, UpstreamErrorFact

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "cookie",
    "key",
    "set-cookie",
    "token",
    "x-api-key",
}


def _sanitize_provider_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if str(key).lower() in _SENSITIVE_KEYS
                else _sanitize_provider_value(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_sanitize_provider_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "...[truncated]"
    return value


def upstream_error_payload(exc: Exception) -> dict[str, Any] | None:
    """Extract a bounded, sanitized upstream HTTP error for diagnostics."""
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = exc.response
    request_ids = {
        name: response.headers[name]
        for name in (
            "x-request-id",
            "request-id",
            "cf-ray",
            "x-amzn-requestid",
        )
        if response.headers.get(name)
    }
    try:
        body: Any = response.json()
    except (httpx.ResponseNotRead, httpx.StreamClosed):
        body = "[upstream response body was not read]"
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        try:
            body = response.text
        except (httpx.ResponseNotRead, httpx.StreamClosed):
            body = "[upstream response body was not read]"
    return {
        "status_code": response.status_code,
        "request_ids": request_ids,
        "body": _sanitize_provider_value(body),
    }


_OVERLOAD_ERROR_TYPES = {
    "overloaded",
    "overloaded_error",
    "rate_limit",
    "rate_limit_error",
    "too_many_requests",
}


def is_overload_error_signal(error_type: str | None, message: str | None) -> bool:
    """Whether an upstream stream error event signals overload/rate limiting.

    Only explicit provider overload/rate-limit types (or the canonical
    "overloaded" wording) count; parameter or authentication errors must not
    be mistaken for recoverable overload.
    """
    normalized_type = (error_type or "").strip().lower()
    if normalized_type in _OVERLOAD_ERROR_TYPES or normalized_type.startswith("rate_limit"):
        return True
    return "overloaded" in (message or "").lower()


class UpstreamOverloaded(RuntimeError):
    """An upstream reported overload/rate limiting mid-stream (SSE error event)."""

    def __init__(
        self,
        message: str = "Upstream overloaded",
        *,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.upstream_status: int | None = None


class UpstreamProtocolError(RuntimeError):
    """An upstream response cannot represent a valid, complete result."""

    def __init__(self, message: str, status_code: int = 502, error_type: str = "api_error"):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.upstream_status: int | None = None


def _indicates_missing_model(body: Any) -> bool:
    """Only infer model-not-found when the sanitized provider body says so."""
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    evidence = " ".join(
        str(error.get(name, ""))
        for name in ("code", "type", "message")
    ).lower()
    return "model" in evidence and any(
        marker in evidence
        for marker in ("not found", "not_found", "does not exist", "unknown")
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse an upstream delay or HTTP date without accepting invalid cooldowns."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            # The obsolete HTTP asctime format has no explicit timezone.
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = max(0.0, deadline.timestamp() - time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def normalize_upstream_error(
    exc: Exception,
    *,
    phase: ErrorPhase = ErrorPhase.PROVIDER_REQUEST,
) -> UpstreamErrorFact:
    """Return a stable, sanitized diagnostic fact for an upstream failure."""
    import httpx

    provider_error = upstream_error_payload(exc)
    upstream_status = None
    sanitized_body = None
    provider_request_ids: dict[str, str] = {}

    if provider_error is not None:
        upstream_status = provider_error["status_code"]
        sanitized_body = provider_error["body"]
        provider_request_ids = provider_error["request_ids"]

    retry_after = None
    if isinstance(exc, httpx.TimeoutException):
        code = "upstream_timeout"
        category = ErrorCategory.TIMEOUT
        retry_same_channel = True
        fallback_allowed = True
        message = "Upstream request timed out"
    elif isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            code = "upstream_authentication_failed"
            category = ErrorCategory.AUTHENTICATION_OR_PERMISSION
        elif status_code == 402:
            code = "upstream_quota_exhausted"
            category = ErrorCategory.QUOTA_OR_RATE_LIMIT
        elif status_code == 429:
            code = "upstream_rate_limited"
            category = ErrorCategory.QUOTA_OR_RATE_LIMIT
        elif status_code == 404 and _indicates_missing_model(sanitized_body):
            code = "upstream_model_not_found"
            category = ErrorCategory.MODEL_NOT_FOUND
        elif status_code == 404:
            code = "upstream_resource_not_found"
            category = ErrorCategory.PROTOCOL_OR_PARAMETER_ERROR
        elif status_code in {408, 425}:
            code = "upstream_temporarily_unavailable"
            category = ErrorCategory.UPSTREAM_AVAILABILITY
        elif status_code >= 500:
            code = "upstream_unavailable"
            category = ErrorCategory.UPSTREAM_AVAILABILITY
        else:
            code = "upstream_protocol_or_parameter_error"
            category = ErrorCategory.PROTOCOL_OR_PARAMETER_ERROR
        fallback_allowed = (
            status_code in {401, 402, 403, 404, 408, 409, 425, 429}
            or status_code >= 500
        )
        retry_same_channel = (
            status_code in {408, 409, 425, 429}
            or status_code >= 500
        )
        if fallback_allowed:
            retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
        message = f"Upstream returned HTTP {status_code}"
    elif isinstance(exc, httpx.RequestError):
        code = "upstream_connection_failed"
        category = ErrorCategory.NETWORK_CONNECTIVITY
        retry_same_channel = True
        fallback_allowed = True
        message = "Upstream connection failed"
    elif isinstance(exc, UpstreamProtocolError):
        upstream_status = exc.upstream_status
        code = "upstream_invalid_response"
        category = ErrorCategory.PROTOCOL_OR_PARAMETER_ERROR
        retry_same_channel = False
        fallback_allowed = False
        message = str(exc)
    elif isinstance(exc, UpstreamOverloaded):
        upstream_status = exc.upstream_status
        code = "upstream_overloaded"
        category = ErrorCategory.UPSTREAM_AVAILABILITY
        retry_same_channel = False
        fallback_allowed = True
        message = str(exc)
    else:
        code = "upstream_unknown_error"
        category = ErrorCategory.UNKNOWN
        retry_same_channel = False
        fallback_allowed = False
        message = type(exc).__name__

    return UpstreamErrorFact(
        code=code,
        category=category,
        phase=phase,
        upstream_status=upstream_status,
        retryable=retry_same_channel or fallback_allowed,
        retry_same_channel=retry_same_channel,
        fallback_allowed=fallback_allowed,
        retry_after_seconds=retry_after,
        message=message,
        sanitized_body=sanitized_body,
        provider_request_ids=provider_request_ids,
    )


def format_error_message(exc: Exception) -> str:
    """Format an exception into a non-empty, human-readable string.

    httpx timeout exceptions (ReadTimeout, ConnectTimeout, PoolTimeout) return
    an empty string from ``str(exc)``, which leaves logs and client responses
    with no useful detail.  Always include the exception type name so the
    failure is identifiable.
    """
    provider_error = upstream_error_payload(exc)
    msg = str(exc).strip()
    if provider_error is not None:
        body = json.dumps(provider_error, ensure_ascii=False, separators=(",", ":"))
        return f"{type(exc).__name__}: {msg}; upstream={body}"
    if msg:
        return f"{type(exc).__name__}: {msg}"
    return type(exc).__name__


def classify_error_status(exc: Exception) -> int:
    """Derive the correct proxy HTTP status code from an upstream exception.

    - Timeout  -> 504 Gateway Timeout
    - Upstream 4xx (bad key, bad request, ...) -> pass through
    - Upstream 5xx or connection error -> 502 Bad Gateway
    - Anything else -> 500 Internal Server Error
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return 504
    if isinstance(exc, httpx.HTTPStatusError):
        upstream = exc.response.status_code
        return upstream if 400 <= upstream < 500 else 502
    if isinstance(exc, httpx.RequestError):
        return 502
    if isinstance(exc, UpstreamProtocolError):
        return exc.status_code
    return 500


class APIRouterException(HTTPException):
    """Base exception for API router errors."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class AuthenticationException(APIRouterException):
    """Authentication failed exception."""

    def __init__(self, detail: str = "Authentication failed") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": detail,
                    "type": "authentication_error",
                    "code": "authentication_failed"
                }
            }
        )


class PermissionException(APIRouterException):
    """Permission denied exception."""

    def __init__(self, detail: str = "Permission denied") -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "message": detail,
                    "type": "permission_error",
                    "code": "permission_denied"
                }
            }
        )


class QuotaExceededException(APIRouterException):
    """Quota exceeded exception."""

    def __init__(self, detail: str = "Quota exceeded") -> None:
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "message": detail,
                    "type": "quota_error",
                    "code": "quota_exceeded"
                }
            }
        )


class ModelNotFoundException(APIRouterException):
    """Model not found exception."""

    def __init__(self, model: str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "message": f"Model '{model}' not found",
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }
        )


class InvalidRequestException(APIRouterException):
    """Invalid request exception."""

    def __init__(self, detail: str = "Invalid request") -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": detail,
                    "type": "invalid_request_error",
                    "code": "invalid_request"
                }
            }
        )


class ChannelException(APIRouterException):
    """Channel exception."""

    def __init__(
        self,
        detail: str = "Channel error",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        original_error: Optional[str] = None
    ) -> None:
        error_detail = {
            "error": {
                "message": detail,
                "type": "channel_error",
                "code": "channel_error"
            }
        }
        if original_error:
            error_detail["error"]["original_error"] = original_error
        super().__init__(status_code=status_code, detail=error_detail)


class RateLimitException(APIRouterException):
    """Rate limit exception."""

    def __init__(self, detail: str = "Rate limit exceeded") -> None:
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "message": detail,
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded"
                }
            }
        )


class ChannelsTemporarilyUnavailable(APIRouterException):
    """Compatible channels are cooling down or running a recovery probe."""

    def __init__(self, model: str, retry_after_seconds: int) -> None:
        super().__init__(
            status_code=503,
            detail={"error": {
                "message": f"Channels for model '{model}' are temporarily unavailable",
                "type": "channel_error",
                "code": "channels_temporarily_unavailable",
            }},
            headers={"Retry-After": str(max(1, retry_after_seconds))},
        )


# Exception handlers


async def api_router_exception_handler(request: Request, exc: APIRouterException) -> JSONResponse:
    """Handle API router exceptions."""
    from rotor.core.anthropic_errors import is_anthropic_request, anthropic_error_response
    if is_anthropic_request(request):
        return anthropic_error_response(request, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        headers=exc.headers,
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle general exceptions."""
    logger.exception("Unhandled error for %s %s", request.method, request.url.path, exc_info=exc)
    from rotor.core.anthropic_errors import is_anthropic_request, anthropic_error_response
    if is_anthropic_request(request):
        return anthropic_error_response(request, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "message": "Internal server error",
                "type": "internal_error",
                "code": "internal_error"
            }
        }
    )
