import asyncio
import json
from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typing import Callable
import time
import logging

from rotor.core._response_event import ResponseEventType
from rotor.config import settings
from rotor.observability import performance_metrics

logger = logging.getLogger(__name__)


def _header_value(scope: Scope, name: str) -> str | None:
    """Read a header from the raw ASGI scope (names are lowercase bytes)."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key == wanted:
            return value.decode("latin-1")
    return None


class _StreamObservation:
    """Bounded SSE observation; never buffers or changes outgoing messages."""

    _MAX_EVENT_BYTES = 65536

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.ttft_at: float | None = None
        self.terminal_at: float | None = None
        self.error = False
        self._line = bytearray()
        self._data: list[bytes] = []
        self._event = ""
        self._size = 0
        self._overflow = False
        self._line_non_cr = False
        self._large_event: ResponseEventType | None = None
        self._large_prefix = bytearray()

    def _feed_large_line(self, segment: bytes) -> None:
        if self._large_event is None:
            return
        needed = 5 - len(self._large_prefix)
        self._large_prefix.extend(segment[:needed])
        if self._large_prefix == b"data:":
            self._large_event.feed(segment[needed:])

    def feed(self, body: bytes, now: float) -> None:
        offset = 0
        while offset < len(body):
            end = body.find(b"\n", offset)
            stop = len(body) if end < 0 else end
            length = stop - offset
            self._size += length + (end >= 0)
            self._line_non_cr = self._line_non_cr or bool(body[offset:stop].strip(b"\r"))
            if self._size > self._MAX_EVENT_BYTES and not self._overflow:
                if self.protocol == "responses":
                    self._large_event = ResponseEventType()
                    for line in self._data:
                        self._large_event.feed(line)
                        self._large_event.feed(b"\n")
                    self._feed_large_line(bytes(self._line))
                self._overflow = True
                self._line.clear()
                self._data.clear()
            if self._overflow:
                self._feed_large_line(body[offset:stop])
            else:
                self._line.extend(body[offset:stop])
            if end < 0:
                return
            # An empty physical line ends the event, including CRLF streams.
            empty = not self._line_non_cr
            if empty:
                if not self._overflow:
                    self._dispatch(now)
                elif self._large_event is not None:
                    if self._large_event.valid:
                        event = self._large_event.event_type or self._event
                        self.error |= self._large_event.error or event in {"error", "response.failed", "response.incomplete"}
                        if event == "response.completed":
                            self.terminal_at = now
                    else:
                        self.error = True
                self._large_event = None
                self._data.clear()
                self._event = ""
                self._size = 0
                self._overflow = False
            elif not self._overflow:
                line = bytes(self._line).rstrip(b"\r")
                if line.startswith(b"data:"):
                    self._data.append(line[5:].removeprefix(b" "))
                elif line.startswith(b"event:"):
                    self._event = line[6:].strip().decode("utf-8", errors="replace")
            if self._large_event is not None and self._large_prefix == b"data:":
                self._large_event.feed(b"\n")
            self._large_prefix.clear()
            self._line.clear()
            self._line_non_cr = False
            offset = end + 1

    def _dispatch(self, now: float) -> None:
        if not self._data:
            return
        raw = b"\n".join(self._data)
        if raw == b"[DONE]":
            if self.protocol == "chat":
                self.terminal_at = now
            return
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.error = True
            return
        if not isinstance(value, dict):
            self.error = True
            return
        event = value.get("type") or self._event
        if not isinstance(event, str):
            event = ""
        if value.get("error") or event in {"error", "response.failed", "response.incomplete"}:
            self.error = True
        if event == "message_stop" and self.protocol == "anthropic":
            self.terminal_at = now
        if event == "response.completed" and self.protocol == "responses":
            self.terminal_at = now
        text = None
        if self.protocol == "chat":
            choices = value.get("choices")
            for choice in choices if isinstance(choices, list) else []:
                if isinstance(choice, dict):
                    delta = choice.get("delta")
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str) and delta["content"]:
                        text = delta["content"]
                        break
        elif self.protocol == "responses" and event == "response.output_text.delta":
            text = value.get("delta")
        elif self.protocol == "anthropic" and event == "content_block_delta":
            delta = value.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
        if self.ttft_at is None and isinstance(text, str) and text:
            self.ttft_at = now


class LoggingMiddleware:
    """Middleware for logging requests and responses."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Log HTTP response start without buffering or cancelling streams."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        started_at = time.monotonic()
        method = scope.get("method", "")
        path = scope.get("path", "")
        protocol = {
            f"{settings.API_V1_STR}/chat/completions": "chat",
            f"{settings.API_V1_STR}/responses": "responses",
            f"{settings.API_V1_STR}/images/generations": "images",
            "/anthropic/v1/messages": "anthropic",
        }.get(path) if method == "POST" else None
        status_code = 0
        observation = None
        completed_at = None
        cancelled = False
        client = scope.get("client")
        client_host = client[0] if client else "unknown"
        user_agent = _header_value(scope, "user-agent") or "-"

        logger.info(
            "Request: %s %s from %s UA=%s",
            method,
            path,
            client_host,
            user_agent[:200],
        )

        async def send_with_logging(message: Message) -> None:
            nonlocal status_code, observation, completed_at, cancelled
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if protocol and any(
                    key.lower() == b"content-type" and b"text/event-stream" in value.lower()
                    for key, value in message.get("headers", [])
                ):
                    observation = _StreamObservation(protocol)
                duration = time.time() - start_time
                logger.info(
                    "Response: %s for %s %s in %.3fs",
                    message["status"],
                    method,
                    path,
                    duration,
                )
                MutableHeaders(scope=message)["X-Process-Time"] = str(duration)
            try:
                await send(message)
            except OSError:
                cancelled = completed_at is None
                raise
            if protocol and message["type"] == "http.response.body":
                now = time.monotonic()
                if observation is not None:
                    observation.feed(message.get("body", b""), now)
                if not message.get("more_body", False):
                    completed_at = now

        async def receive_observed() -> Message:
            nonlocal cancelled
            message = await receive()
            if message["type"] == "http.disconnect" and completed_at is None:
                cancelled = True
            return message

        try:
            await self.app(scope, receive_observed if protocol else receive, send_with_logging)
        except asyncio.CancelledError:
            cancelled = completed_at is None
            raise
        finally:
            if protocol:
                finished_at = observation.terminal_at if observation is not None else completed_at
                if finished_at is not None:
                    cancelled = False
                success = (
                    200 <= status_code < 300 and finished_at is not None and not cancelled
                    and (observation is None or not observation.error)
                )
                performance_metrics.request(
                    protocol=protocol, success=success, cancelled=cancelled,
                    stream=observation is not None,
                    latency_ms=((finished_at or time.monotonic()) - started_at) * 1000,
                    ttft_ms=(
                        (observation.ttft_at - started_at) * 1000
                        if observation is not None and observation.ttft_at is not None else None
                    ),
                )


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
