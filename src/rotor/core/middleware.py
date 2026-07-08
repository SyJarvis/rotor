from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from typing import Callable
import time
import logging

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for logging requests and responses."""

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log details."""
        start_time = time.time()

        # Log request
        logger.info(
            f"Request: {request.method} {request.url.path} "
            f"from {request.client.host if request.client else 'unknown'}"
        )

        # Process request
        response = await call_next(request)

        # Calculate duration
        duration = time.time() - start_time

        # Log response
        logger.info(
            f"Response: {response.status_code} for {request.method} {request.url.path} "
            f"in {duration:.3f}s"
        )

        # Add timing header
        response.headers["X-Process-Time"] = str(duration)

        return response


class CORSMiddleware(BaseHTTPMiddleware):
    """Custom CORS middleware."""

    def __init__(
        self,
        app: ASGIApp,
        allow_origins: list[str] | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
    ):
        super().__init__(app)
        self.allow_origins = allow_origins or ["*"]
        self.allow_methods = allow_methods or ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
        self.allow_headers = allow_headers or ["*"]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and add CORS headers."""
        # Handle preflight requests
        if request.method == "OPTIONS":
            response = Response()
        else:
            response = await call_next(request)

        # Add CORS headers
        origin = request.headers.get("origin")

        if origin in self.allow_origins or "*" in self.allow_origins:
            response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
            response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
            response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
            response.headers["Access-Control-Allow-Credentials"] = "true"

        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple rate limiting middleware (in-memory)."""

    def __init__(self, app: ASGIApp, requests_per_minute: int = 60):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.request_counts: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting."""
        # Skip rate limiting for admin endpoints
        if request.url.path.startswith("/api/admin"):
            return await call_next(request)

        client_id = request.client.host if request.client else "unknown"
        current_time = time.time()

        # Clean old requests
        if client_id in self.request_counts:
            self.request_counts[client_id] = [
                t for t in self.request_counts[client_id]
                if current_time - t < 60
            ]
        else:
            self.request_counts[client_id] = []

        # Check rate limit
        if len(self.request_counts[client_id]) >= self.requests_per_minute:
            from fastapi import status
            from rotor.core.exceptions import RateLimitException
            raise RateLimitException(
                f"Rate limit exceeded: {self.requests_per_minute} requests per minute"
            )

        # Add current request
        self.request_counts[client_id].append(current_time)

        return await call_next(request)
