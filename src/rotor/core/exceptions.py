import json
import logging
from typing import Any, Optional
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

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


# Exception handlers


async def api_router_exception_handler(request: Request, exc: APIRouterException) -> JSONResponse:
    """Handle API router exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle general exceptions."""
    logger.exception("Unhandled error for %s %s", request.method, request.url.path, exc_info=exc)
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
