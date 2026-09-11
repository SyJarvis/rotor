"""Gateway observations must preserve streaming bytes and completion semantics."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from rotor.core import middleware


class RecordingMetrics:
    def __init__(self):
        self.requests = []

    def request(self, **values):
        self.requests.append(values)


def _sse(value):
    return b"data: " + json.dumps(value).encode() + b"\r\n\r\n"


def _exercise(monkeypatch, chunks, *, path="/v1/chat/completions", content_type=b"text/event-stream",
              status=200, cancel=False, disconnect=False, method="POST"):
    recorded = RecordingMetrics()
    monkeypatch.setattr(middleware, "performance_metrics", recorded)
    clock = [10.0]
    monkeypatch.setattr(middleware, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type)]})
        for chunk in chunks:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        if cancel:
            raise asyncio.CancelledError()
        if disconnect:
            assert (await receive())["type"] == "http.disconnect"
            return
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        clock[0] += 1
        if message["type"] == "http.response.body":
            sent.append(message["body"])

    async def receive():
        return {"type": "http.disconnect"}

    async def run():
        call = middleware.LoggingMiddleware(app)(
            {"type": "http", "method": method, "path": path, "headers": []}, receive, send,
        )
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            await call

    asyncio.run(run())
    assert b"".join(sent) == b"".join(chunks)
    return recorded.requests


@pytest.mark.parametrize(("path", "prefix", "text", "terminal"), [
    ("/v1/chat/completions", {"choices": [{"delta": {"role": "assistant"}}]},
     {"choices": [{"delta": {"content": "hello"}}]}, b"data: [DONE]\r\n\r\n"),
    ("/v1/responses", {"type": "response.created"},
     {"type": "response.output_text.delta", "delta": "hello"}, _sse({"type": "response.completed"})),
    ("/anthropic/v1/messages", {"type": "message_start"},
     {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
     _sse({"type": "message_stop"})),
])
def test_fragmented_stream_counts_first_text_and_terminal(monkeypatch, path, prefix, text, terminal):
    fragment = _sse(text)
    result, = _exercise(monkeypatch, [_sse(prefix), fragment[:9], fragment[9:-1], fragment[-1:], terminal], path=path)
    assert result["success"] is True
    assert result["cancelled"] is False
    assert result["ttft_ms"] == 5000  # Headers and textless/partial frames do not count.
    assert result["latency_ms"] == 6000  # Terminal, not the later empty body.


def test_tool_only_stream_has_no_ttft(monkeypatch):
    result, = _exercise(monkeypatch, [_sse({"choices": [{"delta": {"tool_calls": [{"id": "x"}]}}]}),
                                     b"data: [DONE]\n\n"])
    assert result["success"] is True
    assert result["ttft_ms"] is None


@pytest.mark.parametrize("chunks", [
    [_sse({"error": {"message": "upstream failed"}}), b"data: [DONE]\n\n"],
    [_sse({"choices": [{"delta": {"content": "unfinished"}}]})],
])
def test_http_200_stream_error_or_missing_terminal_is_not_success(monkeypatch, chunks):
    result, = _exercise(monkeypatch, chunks)
    assert result["success"] is False
    assert result["cancelled"] is False


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_cancel_before_terminal_is_distinct_from_client_closing_completed_stream(monkeypatch, terminal, disconnect):
    chunks = [b"data: [DONE]\n\n"] if terminal else []
    result, = _exercise(monkeypatch, chunks, cancel=not disconnect, disconnect=disconnect)
    assert result["success"] is terminal
    assert result["cancelled"] is not terminal


def test_oversized_frame_is_skipped_without_changing_bytes_or_later_success(monkeypatch):
    oversized = b"data: " + b"x" * 100000
    result, = _exercise(monkeypatch, [oversized[:60000], oversized[60000:], b"\n\n", b"data: [DONE]\n\n"])
    assert result["success"] is True
    assert result["ttft_ms"] is None
    observer = middleware._StreamObservation("chat")
    observer.feed(oversized, 0)
    assert len(observer._line) <= observer._MAX_EVENT_BYTES
    assert not observer._data


def test_images_nonstream_uses_body_completion_and_no_ttft(monkeypatch):
    result, = _exercise(monkeypatch, [b'{"data":[]}'], path="/v1/images/generations", content_type=b"application/json")
    assert result["success"] is True
    assert result["stream"] is False
    assert result["latency_ms"] == 3000
    assert result["ttft_ms"] is None


@pytest.mark.parametrize("path,method", [("/health", "POST"), ("/api/admin/monitoring", "GET"),
                                         ("/v1/models", "GET"), ("/v1/chat/completions", "OPTIONS")])
def test_non_inference_routes_are_excluded(monkeypatch, path, method):
    assert _exercise(monkeypatch, [b"{}"], path=path, method=method) == []


@pytest.mark.parametrize("payload", [b"not-json", b'[]', b'{"choices":1,"type":[]}', b'[' * 1100])
def test_unexpected_sse_payload_does_not_interrupt_forwarding(monkeypatch, payload):
    result, = _exercise(monkeypatch, [b"data: " + payload + b"\n\n", b"data: [DONE]\n\n"])
    assert result["ttft_ms"] is None


def test_http_error_response_is_failed_without_text_timing(monkeypatch):
    result, = _exercise(monkeypatch, [b'{"error":"unauthorized"}'],
                        status=401, content_type=b"application/json")
    assert result["success"] is False
    assert result["cancelled"] is False
    assert result["ttft_ms"] is None


@pytest.mark.parametrize("kind", ["text", "arguments"])
@pytest.mark.parametrize("type_last", [False, True])
@pytest.mark.parametrize("disconnect", [False, True])
def test_large_responses_completion_is_success(monkeypatch, kind, type_last, disconnect):
    output = [{kind: ('large \\" text ☃ ' * 9000), "type": "response.failed"}]
    value = {"response": {"output": output}, "type": "response.completed"} if type_last else {
        "type": "response.completed", "response": {"output": output},
    }
    event = _sse(value)
    chunks = [event[index:index + 997] for index in range(0, len(event), 997)]
    result, = _exercise(monkeypatch, chunks, path="/v1/responses", disconnect=disconnect)
    assert result["success"] is True
    assert result["cancelled"] is False
    assert result["ttft_ms"] is None


@pytest.mark.parametrize("event_type", ["response.failed", "response.incomplete", "error"])
def test_large_responses_error_is_not_masked_by_later_completed(monkeypatch, event_type):
    event = _sse({"error": {"message": "x" * 100000}, "type": event_type})
    result, = _exercise(monkeypatch, [event, _sse({"type": "response.completed"})], path="/v1/responses")
    assert result["success"] is False


@pytest.mark.parametrize("suffix", [b"", b" trailing", b",}"])
def test_large_responses_nested_type_or_invalid_json_is_not_terminal(monkeypatch, suffix):
    value = {"response": {"output": "x" * 100000, "type": "response.completed"}}
    event = b"data: " + json.dumps(value).encode() + suffix + b"\r\n\r\n"
    result, = _exercise(monkeypatch, [event], path="/v1/responses")
    assert result["success"] is False


def test_large_responses_truncated_completion_is_not_terminal(monkeypatch):
    event = b'data: {"type":"response.completed","response":{"output":"' + b"x" * 100000 + b"\n\n"
    result, = _exercise(monkeypatch, [event], path="/v1/responses")
    assert result["success"] is False


@pytest.mark.parametrize("suffix", [b" trailing", b",}", b"{}"])
def test_large_root_completion_with_trailing_garbage_is_not_success(monkeypatch, suffix):
    raw = json.dumps({"type": "response.completed", "response": {"output": "x" * 100000}}).encode()
    result, = _exercise(monkeypatch, [b"data: " + raw + suffix + b"\n\n"], path="/v1/responses")
    assert result["success"] is False


@pytest.mark.parametrize("ending", ["disconnect", "cancel"])
def test_large_completion_followed_by_client_exit_is_success(monkeypatch, ending):
    event = _sse({"type": "response.completed", "response": {"output": "x" * 100000}})
    result, = _exercise(monkeypatch, [event], path="/v1/responses",
                        disconnect=ending == "disconnect", cancel=ending == "cancel")
    assert result["success"] is True
    assert result["cancelled"] is False


def test_large_responses_multiline_data_and_escaped_root_type(monkeypatch):
    escaped_type = "".join(f"\\u{ord(char):04x}" for char in "response.completed").encode()
    event = (b'data: {"response":{"output":"' + b"x" * 100000 + b'"},\r\n'
             b'data: "ty\\u0070e":"' + escaped_type + b'"}\r\n\r\n')
    chunks = [event[:65535], event[65535:-180]] + [bytes([byte]) for byte in event[-180:]]
    result, = _exercise(monkeypatch, chunks, path="/v1/responses")
    assert result["success"] is True


@pytest.mark.parametrize("bad_content", [b'\\z', b'\\u00zz', b'\x01', b'\xff'])
def test_large_completion_with_invalid_string_is_not_success(monkeypatch, bad_content):
    event = (b'data: {"type":"response.completed","response":{"output":"'
             + b"x" * 100000 + bad_content + b'"}}\n\n')
    result, = _exercise(monkeypatch, [event], path="/v1/responses")
    assert result["success"] is False


def test_large_error_object_without_type_is_not_masked_by_completion(monkeypatch):
    event = _sse({"error": {"message": "x" * 100000}})
    result, = _exercise(monkeypatch, [event, _sse({"type": "response.completed"})], path="/v1/responses")
    assert result["success"] is False


def test_large_response_observation_retains_bounded_state():
    observer = middleware._StreamObservation("responses")
    observer.feed(b'data: {"response":{"output":"', 1)
    chunk = b"x" * 4096
    for _ in range(2560):
        observer.feed(chunk, 2)
    assert len(observer._line) <= observer._MAX_EVENT_BYTES
    assert not observer._data
    assert len(observer._large_event._token) <= 256
    assert len(observer._large_event._stack) <= 64
    observer.feed(b'"},"type":"response.completed"}\r\n', 3)
    assert observer.terminal_at is None
    observer.feed(b"\r\n", 4)
    assert observer.terminal_at == 4
    assert observer._large_event is None


def test_excessive_nesting_does_not_escape_observation(monkeypatch):
    event = (b'data: {"type":"response.completed","padding":"' + b"x" * 100000
             + b'","response":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n\n")
    result, = _exercise(monkeypatch, [event], path="/v1/responses")
    assert result["success"] is False
