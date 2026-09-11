"""Protocol-aware asynchronous HTTP clients used by the benchmark runner.

The implementation deliberately speaks HTTP and Server-Sent Events directly;
the optional ``openai`` and ``anthropic`` SDKs are not required.  A client can
be supplied with an ``httpx.AsyncClient`` (or a custom transport), which makes
the same code useful with an in-process ASGI app and with a real gateway.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .metrics import RequestResult


DEFAULT_PROMPT = "Explain what an LLM API gateway does in one concise sentence."
SUPPORTED_PROTOCOLS = ("chat", "responses", "anthropic")


def normalize_protocol(protocol: str) -> str:
    """Normalize protocol names accepted by Rotor and the CLI."""

    value = str(protocol).strip().lower().replace("-", "_")
    aliases = {
        "openai": "chat",
        "openai_chat": "chat",
        "chat_completions": "chat",
        "openai_responses": "responses",
        "response": "responses",
        "anthropic_messages": "anthropic",
        "messages": "anthropic",
    }
    value = aliases.get(value, value)
    if value not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported protocol: {protocol!r}")
    return value


def endpoint_for_protocol(base_url: str, protocol: str) -> str:
    """Resolve a target URL to the endpoint for ``protocol``.

    A Rotor root (``http://host:8000``) gets ``/v1`` (or
    ``/anthropic/v1``).  A direct provider base ending in ``/v1`` is retained.
    Explicit endpoint paths are never duplicated, so channel-style bases such
    as ``http://mock/v1`` work for both the runner and the mock provider.
    """

    proto = normalize_protocol(protocol)
    raw = str(base_url).strip()
    if not raw:
        raise ValueError("base_url must not be empty")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    path = parsed.path.rstrip("/")

    if proto == "chat":
        suffix = "/chat/completions"
        if path.endswith(suffix):
            final_path = path
        elif path.endswith("/v1"):
            final_path = path + suffix
        elif path.endswith("/anthropic"):
            final_path = path + "/v1" + suffix
        else:
            final_path = path + "/v1" + suffix
    elif proto == "responses":
        suffix = "/responses"
        if path.endswith(suffix):
            final_path = path
        elif path.endswith("/v1"):
            final_path = path + suffix
        elif path.endswith("/anthropic"):
            final_path = path + "/v1" + suffix
        else:
            final_path = path + "/v1" + suffix
    else:
        suffix = "/messages"
        if path.endswith(suffix):
            final_path = path
        elif path.endswith("/anthropic/v1") or path.endswith("/v1"):
            final_path = path + suffix
        elif path.endswith("/anthropic"):
            final_path = path + "/v1" + suffix
        elif path == "":
            # Rotor's public Anthropic compatibility route is namespaced;
            # Anthropic's own host exposes the conventional /v1/messages.
            host = (parsed.hostname or "").lower()
            final_path = "/v1/messages" if "anthropic" in host else "/anthropic/v1/messages"
        else:
            final_path = path + "/v1" + suffix

    return urlunsplit((parsed.scheme, parsed.netloc, final_path, parsed.query, ""))


def build_chat_payload(
    model: str,
    prompt: str = DEFAULT_PROMPT,
    *,
    stream: bool = False,
    max_tokens: int = 128,
) -> dict[str, Any]:
    """Construct a stable OpenAI Chat Completions body."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": bool(stream),
    }
    if stream:
        # Ask direct OpenAI-compatible providers for the final usage chunk;
        # Rotor also preserves/normalizes this field when routing upstream.
        payload["stream_options"] = {"include_usage": True}
    return payload


def build_responses_payload(
    model: str,
    prompt: str = DEFAULT_PROMPT,
    *,
    stream: bool = False,
    max_tokens: int = 128,
) -> dict[str, Any]:
    """Construct a stable OpenAI Responses body."""

    return {
        "model": model,
        "instructions": "You are a helpful assistant.",
        "input": prompt,
        "max_output_tokens": max_tokens,
        "temperature": 0,
        "stream": bool(stream),
    }


def build_anthropic_payload(
    model: str,
    prompt: str = DEFAULT_PROMPT,
    *,
    stream: bool = False,
    max_tokens: int = 128,
) -> dict[str, Any]:
    """Construct a stable Anthropic Messages body."""

    return {
        "model": model,
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": bool(stream),
    }


@dataclass(frozen=True)
class SSEEvent:
    """One parsed Server-Sent Event.

    ``data`` is intentionally retained only while a request is in flight; it
    is never copied into :class:`~benchmarks.metrics.RequestResult`.
    """

    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None

    @property
    def type(self) -> str | None:
        """Alias matching the JSON event vocabulary."""

        if self.event:
            return self.event
        try:
            decoded = json.loads(self.data)
        except (TypeError, ValueError):
            return None
        return decoded.get("type") if isinstance(decoded, dict) else None


def _decode_line(line: str | bytes) -> str:
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return str(line)


def parse_sse_lines(lines: Iterable[str | bytes]) -> Iterator[SSEEvent]:
    """Parse SSE lines according to the browser/EventSource framing rules."""

    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None
    retry: int | None = None

    def dispatch() -> SSEEvent | None:
        nonlocal data_lines, event_name, event_id, retry
        if not data_lines:
            # An event field without data is not dispatched by the SSE spec.
            event_name = None
            retry = None
            return None
        event = SSEEvent(
            data="\n".join(data_lines),
            event=event_name,
            event_id=event_id,
            retry=retry,
        )
        data_lines = []
        event_name = None
        retry = None
        return event

    for raw_line in lines:
        line = _decode_line(raw_line).rstrip("\r\n")
        if line == "":
            parsed = dispatch()
            if parsed is not None:
                yield parsed
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator:
            if value.startswith(" "):
                value = value[1:]
        else:
            value = ""
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        elif field == "id":
            # IDs containing NUL are ignored by the SSE spec.
            if "\x00" not in value:
                event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                pass

    parsed = dispatch()
    if parsed is not None:
        yield parsed


def parse_sse(payload: str | bytes) -> list[SSEEvent]:
    """Parse a complete SSE payload (useful in unit tests)."""

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    return list(parse_sse_lines(str(payload).splitlines()))


async def iter_sse_events(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    """Yield parsed events from an ``httpx`` streaming response."""

    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None
    retry: int | None = None

    def dispatch() -> SSEEvent | None:
        nonlocal data_lines, event_name, retry
        if not data_lines:
            event_name = None
            retry = None
            return None
        result = SSEEvent("\n".join(data_lines), event_name, event_id, retry)
        data_lines = []
        event_name = None
        retry = None
        return result

    async for raw_line in response.aiter_lines():
        line = _decode_line(raw_line).rstrip("\r\n")
        if line == "":
            parsed = dispatch()
            if parsed is not None:
                yield parsed
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        elif not separator:
            value = ""
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                pass
    parsed = dispatch()
    if parsed is not None:
        yield parsed


def _event_json(event: SSEEvent) -> dict[str, Any] | None:
    try:
        value = json.loads(event.data)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def event_type(event: SSEEvent) -> str | None:
    """Return an SSE event type from its field or JSON data."""

    if event.event:
        return event.event
    value = _event_json(event)
    event_value = value.get("type") if value else None
    return str(event_value) if event_value else None


def terminal_status(event: SSEEvent, protocol: str) -> str | None:
    """Classify a protocol stream terminal event as ``success``/``failure``."""

    proto = normalize_protocol(protocol)
    data = event.data.strip()
    typ = event_type(event)
    # An SSE ``event:`` label is producer-controlled and may be arbitrary;
    # inspect the JSON ``type`` as a second signal so a hostile label cannot
    # hide a valid terminal marker (or force a false missing-terminal result).
    value = _event_json(event)
    json_type = str(value.get("type")) if value and value.get("type") else None
    terminal_types = {item for item in (typ, json_type) if item}
    if data == "[DONE]" or terminal_types & {"done", "completed"}:
        return "success"
    if terminal_types & {"response.completed", "message_stop"}:
        return "success"
    if terminal_types & {
        "response.failed",
        "response.cancelled",
        "response.canceled",
        "response.incomplete",
        "response.error",
        "error",
        "stream_error",
        "message_error",
    }:
        return "failure"
    # Some providers omit the event field but use an OpenAI-style JSON marker.
    if value:
        status = str(value.get("status", "")).lower()
        if status in {"failed", "error", "cancelled", "canceled"}:
            if proto == "responses" or (typ and typ.startswith("response")):
                return "failure"
        if status == "completed" and proto == "responses":
            return "success"
        if value.get("error") is not None:
            return "failure"
    return None


def is_terminal_event(event: SSEEvent, protocol: str | None = None) -> bool:
    """Whether an event ends a stream (success or failure)."""

    if protocol is None:
        data = event.data.strip()
        typ = event_type(event)
        value = _event_json(event)
        return data == "[DONE]" or typ in {
            "done",
            "completed",
            "response.completed",
            "response.failed",
            "response.cancelled",
            "response.canceled",
            "response.incomplete",
            "response.error",
            "message_stop",
            "error",
            "stream_error",
            "message_error",
        } or bool(value and value.get("error") is not None)
    return terminal_status(event, protocol) is not None


def extract_text_from_event(event: SSEEvent, protocol: str) -> str | None:
    """Extract text-bearing delta fields without retaining the full response."""

    proto = normalize_protocol(protocol)
    value = _event_json(event)
    if not value:
        return None
    if proto == "chat":
        choices = value.get("choices")
        if isinstance(choices, list):
            pieces: list[str] = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    pieces.append(delta["content"])
            return "".join(pieces) or None
    elif proto == "responses":
        typ = event_type(event)
        if typ == "response.output_text.delta" and isinstance(value.get("delta"), str):
            return value["delta"] or None
        # A few Responses-compatible providers use output_text directly.
        for key in ("delta", "text"):
            if isinstance(value.get(key), str) and typ and "text" in typ and value[key]:
                return value[key]
    else:
        delta = value.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return delta["text"] or None
        block = value.get("content_block")
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"] or None
        content = value.get("content")
        if isinstance(content, list):
            pieces = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "".join(pieces) or None
    return None


def extract_usage(value: Mapping[str, Any] | None, protocol: str) -> tuple[int | None, int | None, int | None]:
    """Normalize usage fields to ``(input, output, total)``."""

    if not isinstance(value, Mapping):
        return (None, None, None)
    usage: Mapping[str, Any] | None = value.get("usage") if isinstance(value.get("usage"), Mapping) else None
    if usage is None and isinstance(value.get("response"), Mapping):
        nested = value["response"]
        usage = nested.get("usage") if isinstance(nested.get("usage"), Mapping) else None
    if usage is None and isinstance(value.get("message"), Mapping):
        nested = value["message"]
        usage = nested.get("usage") if isinstance(nested.get("usage"), Mapping) else None
    if usage is None:
        # A native event itself can be a usage object.
        usage = value
    proto = normalize_protocol(protocol)
    if proto == "chat":
        inp = usage.get("prompt_tokens")
        out = usage.get("completion_tokens")
        total = usage.get("total_tokens")
    else:
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        total = usage.get("total_tokens")
    def integer(item: Any) -> int | None:
        try:
            return int(item) if item is not None else None
        except (TypeError, ValueError):
            return None
    inp_i, out_i, total_i = integer(inp), integer(out), integer(total)
    if total_i is None and inp_i is not None and out_i is not None:
        total_i = inp_i + out_i
    return inp_i, out_i, total_i


def _safe_error(category: str, message: str | None = None) -> tuple[str, str]:
    """Keep result errors bounded and free of response bodies/credentials."""

    clean = " ".join(str(message or category).split())
    # Exception strings can contain URLs with query secrets; only expose a
    # short generic message in the result file.
    if len(clean) > 160:
        clean = clean[:157] + "..."
    return category, clean


def _status_category(status_code: int) -> str:
    if status_code == 401 or status_code == 403:
        return "auth"
    if status_code == 408 or status_code == 504:
        return "timeout"
    if status_code == 429:
        return "rate_limit"
    if 400 <= status_code < 500:
        return "http_4xx"
    if status_code >= 500:
        return "http_5xx"
    return "http_error"


class BenchmarkClient:
    """Async HTTP client for one benchmark protocol."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str,
        *,
        timeout: float = 120.0,
        conversation_id: str | None = None,
        random_conversation_id: bool = False,
        max_tokens: int = 128,
        prompt: str = DEFAULT_PROMPT,
        max_connections: int | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.protocol = normalize_protocol(protocol)
        self.url = endpoint_for_protocol(base_url, self.protocol)
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.prompt = prompt
        self.random_conversation_id = bool(random_conversation_id)
        self.conversation_id = conversation_id or f"bench-conv-{uuid.uuid4().hex}"
        if max_connections is not None:
            max_connections = int(max_connections)
            if max_connections <= 0:
                raise ValueError("max_connections must be positive when provided")
        self.max_connections = max_connections
        # httpx defaults to 100 connections.  The runner passes its worker
        # concurrency here so a high-concurrency round is not silently capped
        # by the client pool.  Injected clients are left untouched.
        self.connection_limits = (
            httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            )
            if max_connections is not None
            else None
        )
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=self.connection_limits,
            transport=transport,
        )

    def _conversation_header(self) -> str:
        if self.random_conversation_id:
            return f"bench-conv-{uuid.uuid4().hex}"
        return self.conversation_id

    def _headers(self, stream: bool) -> tuple[str, dict[str, str]]:
        request_id = f"bench-{uuid.uuid4().hex}"
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Conversation-Id": self._conversation_header(),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            if self.protocol == "anthropic":
                # Rotor accepts Bearer tokens on its compatibility route;
                # direct Anthropic endpoints additionally expect x-api-key.
                headers["x-api-key"] = self.api_key
        if self.protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return request_id, headers

    def _payload(self, stream: bool) -> dict[str, Any]:
        if self.protocol == "chat":
            return build_chat_payload(self.model, self.prompt, stream=stream, max_tokens=self.max_tokens)
        if self.protocol == "responses":
            return build_responses_payload(self.model, self.prompt, stream=stream, max_tokens=self.max_tokens)
        return build_anthropic_payload(self.model, self.prompt, stream=stream, max_tokens=self.max_tokens)

    async def request(
        self,
        *,
        stream: bool = False,
        prompt: str | None = None,
        request_index: int | None = None,
    ) -> RequestResult:
        """Send one request and consume it fully, including its SSE terminator."""

        request_id, headers = self._headers(bool(stream))
        conversation_id = headers["X-Conversation-Id"]
        payload = self._payload(stream)
        if prompt is not None:
            # Rebuild only the user text; never retain this body in the result.
            if self.protocol == "chat":
                payload["messages"][-1]["content"] = prompt
            elif self.protocol == "responses":
                payload["input"] = prompt
            else:
                payload["messages"][0]["content"] = prompt

        started_at = time.time()
        started = time.perf_counter()
        status_code: int | None = None
        ttft_ms: float | None = None
        chunk_count = 0
        intervals: list[float] = []
        previous_event_at: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None
        terminal: str | None = None
        terminal_event_name: str | None = None
        error_category: str | None = None
        error_message: str | None = None

        try:
            if stream:
                async with self.http_client.stream(
                    "POST", self.url, headers=headers, json=payload, timeout=self.timeout
                ) as response:
                    status_code = response.status_code
                    if status_code < 200 or status_code >= 300:
                        category, message = _safe_error(_status_category(status_code), f"HTTP {status_code}")
                        error_category, error_message = category, message
                    else:
                        async for event in iter_sse_events(response):
                            now = time.perf_counter()
                            status = terminal_status(event, self.protocol)
                            saw_terminal = status is not None
                            if status is not None:
                                # A failure marker is recorded as a stream
                                # error.  We stop at the first terminal event;
                                # consuming arbitrary bytes after a terminator can hang
                                # forever when an upstream keeps its socket
                                # open, while the body through the terminator
                                # has already been fully accounted for.
                                terminal = (
                                    "failure"
                                    if status == "failure" or terminal == "failure"
                                    else "success"
                                )
                                marker_name = "[DONE]" if event.data.strip() == "[DONE]" else event_type(event)
                                if status == "failure" or terminal_event_name is None:
                                    terminal_event_name = marker_name
                            elif event.data.strip():
                                chunk_count += 1
                                if previous_event_at is not None:
                                    intervals.append((now - previous_event_at) * 1000.0)
                                previous_event_at = now
                            value = _event_json(event)
                            if value:
                                usage = extract_usage(value, self.protocol)
                                # Stream usage is often split across events
                                # (Anthropic message_start/message_delta). Keep
                                # the latest non-null values, allowing a later
                                # positive output count to replace an initial
                                # zero placeholder.
                                if usage[0] is not None:
                                    input_tokens = usage[0]
                                if usage[1] is not None and (
                                    output_tokens is None or usage[1] != 0 or output_tokens == 0
                                ):
                                    output_tokens = usage[1]
                                if usage[2] is not None:
                                    total_tokens = usage[2]
                            text = extract_text_from_event(event, self.protocol)
                            if text is not None and ttft_ms is None:
                                ttft_ms = (now - started) * 1000.0
                            if saw_terminal:
                                break
                        if terminal is None:
                            error_category, error_message = _safe_error(
                                "missing_terminal", "stream ended without a terminal event"
                            )
                        elif terminal == "failure":
                            error_category, error_message = _safe_error(
                                "stream_error", "upstream stream reported failure"
                            )
            else:
                response = await self.http_client.post(
                    self.url, headers=headers, json=payload, timeout=self.timeout
                )
                status_code = response.status_code
                if status_code < 200 or status_code >= 300:
                    error_category, error_message = _safe_error(
                        _status_category(status_code), f"HTTP {status_code}"
                    )
                else:
                    try:
                        value = response.json()
                    except (ValueError, json.JSONDecodeError):
                        value = None
                    if not isinstance(value, dict):
                        error_category, error_message = _safe_error(
                            "protocol", "response was not a JSON object"
                        )
                    elif isinstance(value.get("error"), dict):
                        error_category, error_message = _safe_error(
                            "protocol", "upstream returned an error object"
                        )
                    else:
                        input_tokens, output_tokens, total_tokens = extract_usage(value, self.protocol)
        except httpx.TimeoutException:
            error_category, error_message = _safe_error("timeout", "request timed out")
        except (httpx.RequestError, OSError):
            error_category, error_message = _safe_error("transport", "transport request failed")
        except asyncio.TimeoutError:
            error_category, error_message = _safe_error("timeout", "request timed out")
        except Exception:
            # Do not serialize exception text: it can contain URLs, headers,
            # or provider response data.
            error_category, error_message = _safe_error("client_error", "client request failed")

        finished = time.perf_counter()
        finished_at = time.time()
        duration_ms = max(0.0, (finished - started) * 1000.0)
        success = error_category is None
        # A stream with no explicit error is successful only after a terminal
        # success marker.  Non-stream HTTP 2xx JSON is successful above.
        if stream and terminal != "success":
            success = False
            if error_category is None:
                error_category, error_message = _safe_error(
                    "missing_terminal", "stream ended without a successful terminal event"
                )
        if input_tokens is not None and output_tokens is not None:
            # A provider may derive a partial total on ``message_start``
            # (input + output=0) and later send the real output count.  At
            # stream close the two component counters are authoritative for
            # the benchmark, while a provider-supplied equal total is kept.
            component_total = input_tokens + output_tokens
            if total_tokens is None or total_tokens != component_total:
                total_tokens = component_total
        rate = None
        if output_tokens is not None and duration_ms > 0:
            rate = float(output_tokens) / (duration_ms / 1000.0)
        return RequestResult(
            request_id=request_id,
            conversation_id=conversation_id,
            protocol=self.protocol,
            stream=bool(stream),
            success=success,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            chunk_count=chunk_count,
            chunk_intervals_ms=intervals,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            output_tokens_per_s=rate,
            status_code=status_code,
            terminal_event=terminal_event_name,
            error_category=error_category,
            error_message=error_message,
            started_at=started_at,
            finished_at=finished_at,
            request_index=request_index,
        )

    async def send(self, **kwargs: Any) -> RequestResult:
        """Alias for :meth:`request`."""

        return await self.request(**kwargs)

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def __aenter__(self) -> "BenchmarkClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()


class OpenAIChatClient(BenchmarkClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, protocol="chat", **kwargs)


class OpenAIResponsesClient(BenchmarkClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, protocol="responses", **kwargs)


class AnthropicMessagesClient(BenchmarkClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, protocol="anthropic", **kwargs)


# Common short aliases.
ChatClient = OpenAIChatClient
ResponsesClient = OpenAIResponsesClient
AnthropicClient = AnthropicMessagesClient


__all__ = [
    "AnthropicClient",
    "AnthropicMessagesClient",
    "BenchmarkClient",
    "ChatClient",
    "DEFAULT_PROMPT",
    "OpenAIChatClient",
    "OpenAIResponsesClient",
    "ResponsesClient",
    "SSEEvent",
    "SUPPORTED_PROTOCOLS",
    "build_anthropic_payload",
    "build_chat_payload",
    "build_responses_payload",
    "endpoint_for_protocol",
    "event_type",
    "extract_text_from_event",
    "extract_usage",
    "is_terminal_event",
    "iter_sse_events",
    "normalize_protocol",
    "parse_sse",
    "parse_sse_lines",
    "terminal_status",
]
