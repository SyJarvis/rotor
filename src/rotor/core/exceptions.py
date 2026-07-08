from typing import Any, Optional
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


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
