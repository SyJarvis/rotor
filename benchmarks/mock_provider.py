"""Deterministic local upstream for offline benchmark and integration tests.

Run it with::

    python -m benchmarks.mock_provider --port 9001 --chunk-interval 0.01

The service intentionally has no model or credential dependency.  It supports
the endpoint shapes used by Rotor's OpenAI Chat, OpenAI Responses and
Anthropic adapters.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import math
import os
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass(frozen=True)
class MockProviderConfig:
    """Runtime knobs for repeatable JSON/SSE responses."""

    first_chunk_delay: float = 0.0
    chunk_interval: float = 0.0
    chunk_count: int = 3
    status_code: int = 200
    response_text: str = "mock provider response"
    input_tokens: int = 8
    output_tokens: int | None = None
    # Deterministic sequence fault injection.  When ``fail_after`` is set,
    # requests with sequence numbers greater than that value fail until (and
    # including) ``recover_after`` when provided.  Thus
    # ``fail_after=2,recover_after=4`` yields success, success, failure,
    # failure, success, ... .  A missing ``fail_after`` with a recovery value
    # starts failure at request 1 and recovers after the requested sequence.
    fail_after: int | None = None
    recover_after: int | None = None
    failure_status_code: int = 503
    # Constructor alias retained for callers that use the shorter spelling.
    fail_status_code: int | None = None

    @classmethod
    def from_env(cls) -> "MockProviderConfig":
        def number(name: str, default: float) -> float:
            try:
                return max(0.0, float(os.getenv(name, default)))
            except (TypeError, ValueError):
                return default

        try:
            chunks = max(1, int(os.getenv("MOCK_CHUNK_COUNT", "3")))
        except (TypeError, ValueError):
            chunks = 3
        try:
            status = int(os.getenv("MOCK_STATUS_CODE", "200"))
        except (TypeError, ValueError):
            status = 200
        try:
            input_tokens = max(0, int(os.getenv("MOCK_INPUT_TOKENS", "8")))
        except (TypeError, ValueError):
            input_tokens = 8
        raw_output = os.getenv("MOCK_OUTPUT_TOKENS")
        try:
            output_tokens = max(0, int(raw_output)) if raw_output is not None else None
        except (TypeError, ValueError):
            output_tokens = None
        def optional_int(name: str) -> int | None:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        try:
            failure_status = int(
                os.getenv("MOCK_FAILURE_STATUS_CODE", os.getenv("MOCK_FAIL_STATUS_CODE", "503"))
            )
        except (TypeError, ValueError):
            failure_status = 503
        return cls(
            first_chunk_delay=number("MOCK_FIRST_CHUNK_DELAY", 0.0),
            chunk_interval=number("MOCK_CHUNK_INTERVAL", 0.0),
            chunk_count=chunks,
            status_code=status,
            response_text=os.getenv("MOCK_RESPONSE_TEXT", "mock provider response"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            fail_after=optional_int("MOCK_FAIL_AFTER"),
            recover_after=optional_int("MOCK_RECOVER_AFTER"),
            failure_status_code=failure_status,
        )

    def validated(self) -> "MockProviderConfig":
        try:
            first_delay = float(self.first_chunk_delay)
            interval = float(self.chunk_interval)
            chunks = int(self.chunk_count)
            status = int(self.status_code)
            input_tokens = int(self.input_tokens)
            output_tokens = int(self.output_tokens) if self.output_tokens is not None else None
            fail_after = int(self.fail_after) if self.fail_after is not None else None
            recover_after = int(self.recover_after) if self.recover_after is not None else None
            failure_status = int(
                self.fail_status_code
                if self.fail_status_code is not None
                else self.failure_status_code
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("mock numeric settings must be valid numbers") from exc
        if not math.isfinite(first_delay) or not math.isfinite(interval):
            raise ValueError("mock delays must be finite")
        if first_delay < 0 or interval < 0:
            raise ValueError("mock delays must be non-negative")
        if chunks <= 0:
            raise ValueError("chunk_count must be positive")
        if input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        if output_tokens is not None and output_tokens < 0:
            raise ValueError("output_tokens must be non-negative")
        if not 100 <= status <= 599:
            raise ValueError("status_code must be a valid HTTP status")
        if fail_after is not None and fail_after < 0:
            raise ValueError("fail_after must be non-negative when provided")
        if recover_after is not None and recover_after < 0:
            raise ValueError("recover_after must be non-negative when provided")
        if not 100 <= failure_status <= 599 or 200 <= failure_status < 300:
            raise ValueError("failure_status_code must be a non-2xx HTTP status")
        failure_start = 1 if fail_after is None else fail_after + 1
        if recover_after is not None and recover_after < failure_start:
            raise ValueError("recover_after must be at least fail_after + 1")
        return replace(
            self,
            first_chunk_delay=first_delay,
            chunk_interval=interval,
            chunk_count=chunks,
            status_code=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_text=str(self.response_text),
            fail_after=fail_after,
            recover_after=recover_after,
            failure_status_code=failure_status,
            fail_status_code=None,
        )

    def failure_for_sequence(self, sequence: int) -> tuple[int, str] | None:
        """Return ``(status, category)`` for an injected sequence failure.

        The fixed ``status_code`` setting is handled by the request handler for
        backwards compatibility.  This helper only describes the optional
        sequence schedule and is public so tests/callers can inspect it
        without making HTTP requests.
        """

        # Callers may inspect a freshly constructed config (without an
        # explicit ``validated()`` call).  Normalize aliases and enforce the
        # same schedule/status invariants before evaluating the sequence.
        config = self.validated()
        try:
            number = int(sequence)
        except (TypeError, ValueError):
            raise ValueError("sequence must be an integer") from None
        if number <= 0:
            raise ValueError("sequence must be positive")
        start = 1 if config.fail_after is None else config.fail_after + 1
        if config.fail_after is None and config.recover_after is None:
            return None
        end = config.recover_after
        if number < start or (end is not None and number > end):
            return None
        return (config.failure_status_code, "injected_sequence_failure")

    # A descriptive alias for code that calls the schedule a status policy.
    status_for_sequence = failure_for_sequence


def _split_text(text: str, count: int) -> list[str]:
    if count <= 1:
        return [text]
    # Fixed index arithmetic, rather than a tokenizer, ensures identical
    # chunks on every Python/platform run.
    length = len(text)
    return [text[(i * length) // count : ((i + 1) * length) // count] for i in range(count)]


def _error_body(
    status: int,
    *,
    category: str = "fixed_status",
    sequence: int | None = None,
) -> dict[str, Any]:
    """Build a safe, machine-readable mock error response.

    ``category`` distinguishes a fixed configured status from a sequence fault
    while retaining the original ``mock_http_<status>`` code for callers that
    relied on the first-stage behavior.
    """

    code = f"mock_http_{status}" if category == "fixed_status" else f"mock_{category}"
    body: dict[str, Any] = {
        "error": {
            "type": "mock_error",
            "category": category,
            "code": code,
            "message": (
                f"mock provider configured HTTP {status}"
                if category == "fixed_status"
                else f"mock provider injected {category} at request sequence {sequence}"
            ),
        }
    }
    if sequence is not None:
        body["error"]["request_sequence"] = sequence
    return body


def _model(body: Mapping[str, Any]) -> str:
    value = body.get("model", "mock-model")
    return str(value)


def _chat_json(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> dict[str, Any]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    total = config.input_tokens + output
    return {
        "id": f"chatcmpl-mock-{sequence:08d}",
        "object": "chat.completion",
        "created": 1700000000,
        "model": _model(body),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": config.input_tokens,
            "completion_tokens": output,
            "total_tokens": total,
        },
    }


def _responses_json(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> dict[str, Any]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    return {
        "id": f"resp-mock-{sequence:08d}",
        "object": "response",
        "created_at": 1700000000,
        "status": "completed",
        "model": _model(body),
        "output": [{
            "id": f"msg-mock-{sequence:08d}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {
            "input_tokens": config.input_tokens,
            "output_tokens": output,
            "total_tokens": config.input_tokens + output,
        },
    }


def _anthropic_json(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> dict[str, Any]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    return {
        "id": f"msg-mock-{sequence:08d}",
        "type": "message",
        "role": "assistant",
        "model": _model(body),
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": config.input_tokens,
            "output_tokens": output,
        },
    }


def _sse(data: Any, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    import json

    return f"{prefix}data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _delayed_events(
    events: list[str],
    config: MockProviderConfig,
    *,
    first_text_index: int = 0,
    text_indexes: set[int] | None = None,
) -> AsyncIterator[str]:
    """Yield events with delay anchored to the first *text* event.

    Protocols such as Responses and Anthropic have metadata preambles.  Those
    preambles are emitted immediately; the configured first delay models time
    to first useful text consistently across protocols.
    """

    text_indexes = text_indexes or set()
    for index, event in enumerate(events):
        if index == first_text_index and config.first_chunk_delay:
            await asyncio.sleep(config.first_chunk_delay)
        elif index in text_indexes and index > first_text_index and config.chunk_interval:
            await asyncio.sleep(config.chunk_interval)
        yield event


def _chat_stream(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> list[str]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    events: list[str] = []
    for index, piece in enumerate(_split_text(text, config.chunk_count)):
        delta: dict[str, Any] = {"content": piece}
        if index == 0:
            delta = {"role": "assistant", "content": piece}
        events.append(_sse({
            "id": f"chatcmpl-mock-{sequence:08d}",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": _model(body),
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }))
    events.append(_sse({
        "id": f"chatcmpl-mock-{sequence:08d}",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": _model(body),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": config.input_tokens, "completion_tokens": output, "total_tokens": config.input_tokens + output},
    }))
    events.append("data: [DONE]\n\n")
    return events


def _responses_stream(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> list[str]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    response_stub = {
        "id": f"resp-mock-{sequence:08d}",
        "object": "response",
        "created_at": 1700000000,
        "status": "in_progress",
        "model": _model(body),
        "output": [],
    }
    events = [
        _sse({"type": "response.created", "response": response_stub}),
        _sse({"type": "response.in_progress", "response": response_stub}),
    ]
    for piece in _split_text(text, config.chunk_count):
        events.append(_sse({"type": "response.output_text.delta", "delta": piece}))
    events.extend([
        _sse({"type": "response.output_text.done", "text": text}),
        _sse({
            "type": "response.completed",
            "response": {
                **response_stub,
                "status": "completed",
                "usage": {
                    "input_tokens": config.input_tokens,
                    "output_tokens": output,
                    "total_tokens": config.input_tokens + output,
                },
            },
        }),
    ])
    return events


def _anthropic_stream(body: Mapping[str, Any], config: MockProviderConfig, sequence: int) -> list[str]:
    text = config.response_text
    output = config.output_tokens if config.output_tokens is not None else max(1, len(text.split()))
    events = [
        _sse({"type": "message_start", "message": {
            "id": f"msg-mock-{sequence:08d}", "type": "message", "role": "assistant",
            "model": _model(body), "content": [], "stop_reason": None,
            "usage": {"input_tokens": config.input_tokens, "output_tokens": 0},
        }}, "message_start"),
        _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
    ]
    for piece in _split_text(text, config.chunk_count):
        events.append(_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}, "content_block_delta"))
    events.extend([
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": output}}, "message_delta"),
        _sse({"type": "message_stop"}, "message_stop"),
    ])
    return events


def create_app(config: MockProviderConfig | None = None) -> FastAPI:
    """Create an isolated FastAPI app, suitable for ``httpx.ASGITransport`` tests."""

    cfg = (config or MockProviderConfig.from_env()).validated()
    app = FastAPI(title="Rotor benchmark mock provider")
    sequence = itertools.count(1)

    async def handle(request: Request, protocol: str):
        body = await request.json()
        if not isinstance(body, Mapping):
            return JSONResponse(_error_body(400), status_code=400)
        if cfg.status_code != 200:
            return JSONResponse(_error_body(cfg.status_code), status_code=cfg.status_code)
        request_sequence = next(sequence)
        injected_failure = cfg.failure_for_sequence(request_sequence)
        if injected_failure is not None:
            status, category = injected_failure
            return JSONResponse(
                _error_body(status, category=category, sequence=request_sequence),
                status_code=status,
            )
        stream = bool(body.get("stream", False))
        if not stream:
            # Reuse the first-chunk knob as a deterministic response delay for
            # JSON calls too; this makes timeout/error scenarios reproducible
            # without introducing a second nearly-identical setting.
            if cfg.first_chunk_delay:
                await asyncio.sleep(cfg.first_chunk_delay)
            if protocol == "chat":
                return JSONResponse(_chat_json(body, cfg, request_sequence))
            if protocol == "responses":
                return JSONResponse(_responses_json(body, cfg, request_sequence))
            return JSONResponse(_anthropic_json(body, cfg, request_sequence))
        if protocol == "chat":
            events = _chat_stream(body, cfg, request_sequence)
        elif protocol == "responses":
            events = _responses_stream(body, cfg, request_sequence)
        else:
            events = _anthropic_stream(body, cfg, request_sequence)
        if protocol == "chat":
            first_text_index = 0
            text_indexes = set(range(cfg.chunk_count))
        elif protocol == "responses":
            first_text_index = 2
            text_indexes = set(range(2, 2 + cfg.chunk_count))
        else:
            first_text_index = 2
            text_indexes = set(range(2, 2 + cfg.chunk_count))
        return StreamingResponse(
            _delayed_events(
                events,
                cfg,
                first_text_index=first_text_index,
                text_indexes=text_indexes,
            ),
            media_type="text/event-stream",
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await handle(request, "chat")

    @app.post("/chat/completions")
    async def chat_short(request: Request):
        return await handle(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await handle(request, "responses")

    @app.post("/responses")
    async def responses_short(request: Request):
        return await handle(request, "responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await handle(request, "anthropic")

    @app.post("/messages")
    async def messages_short(request: Request):
        return await handle(request, "anthropic")

    @app.post("/anthropic/v1/messages")
    async def messages_rotor_prefix(request: Request):
        return await handle(request, "anthropic")

    return app


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a deterministic local benchmark upstream")
    parser.add_argument("--host", default=os.getenv("MOCK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOCK_PORT", "9001")))
    parser.add_argument("--first-chunk-delay", type=float, default=None)
    parser.add_argument("--chunk-interval", type=float, default=None)
    parser.add_argument("--chunk-count", type=int, default=None)
    parser.add_argument("--status-code", type=int, default=None, help="Return e.g. 429/500/503")
    parser.add_argument("--response-text", default=None)
    parser.add_argument("--input-tokens", type=int, default=None)
    parser.add_argument("--output-tokens", type=int, default=None)
    parser.add_argument(
        "--fail-after",
        type=int,
        default=None,
        help="Begin injected failures after this many successful requests",
    )
    parser.add_argument(
        "--recover-after",
        type=int,
        default=None,
        help="Stop sequence failures after this request number (inclusive)",
    )
    parser.add_argument(
        "--failure-status-code",
        "--fail-status-code",
        dest="failure_status_code",
        type=int,
        default=None,
        help="HTTP status for sequence-injected failures (default 503)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = MockProviderConfig.from_env()
    overrides = {
        key: value for key, value in {
            "first_chunk_delay": args.first_chunk_delay,
            "chunk_interval": args.chunk_interval,
            "chunk_count": args.chunk_count,
            "status_code": args.status_code,
            "response_text": args.response_text,
            "input_tokens": args.input_tokens,
            "output_tokens": args.output_tokens,
            "fail_after": args.fail_after,
            "recover_after": args.recover_after,
            "failure_status_code": args.failure_status_code,
        }.items() if value is not None
    }
    cfg = replace(base, **overrides).validated()
    import uvicorn

    # Pass the app object so CLI overrides are honored.
    uvicorn.run(create_app(cfg), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["MockProviderConfig", "app", "create_app", "build_parser", "main"]
