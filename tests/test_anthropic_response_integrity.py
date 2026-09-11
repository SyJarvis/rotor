import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
from httpx import AsyncClient, ASGITransport, MockTransport
import pytest

import rotor.api.v1.anthropic as endpoint
from rotor.core.deps import get_current_token
from rotor.core.exceptions import AuthenticationException, ChannelsTemporarilyUnavailable
from rotor.database import get_db
from rotor.main import app as configured_app
from rotor.models.log import RequestLog
from rotor.models.request_attempt import RequestAttempt
from rotor.models.usage import UsageLedger
from tests.test_attempt_latency import environment


def message(**fields):
    return {"id": "msg_actual", "type": "message", "role": "assistant", "model": "model",
            "content": [], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}, **fields}


def native_events():
    return [
        {"type": "message_start", "message": message(stop_reason=None)},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]


def wire(events, *, multiline=False):
    chunks = []
    for event in events:
        text = json.dumps(event, indent=2 if multiline else None)
        if multiline:
            chunks.append(": heartbeat\nevent: " + event["type"] + "\n" + "\n".join("data:" + line for line in text.splitlines()) + "\n\n")
        else:
            chunks.append("data: " + text + "\n\n")
    return "".join(chunks)


@pytest.fixture
def api(environment, monkeypatch):
    env = environment
    env.channels[:] = env.channels[:1]
    env.channels[0].type = "zhipu"
    env.channels[0].protocol = "anthropic"
    app = FastAPI()
    app.exception_handlers.update(configured_app.exception_handlers)
    app.include_router(endpoint.router, prefix="/v1")
    app.include_router(endpoint.router, prefix="/anthropic/v1")

    async def database():
        yield env.db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_token] = lambda: env.token
    monkeypatch.setattr(env.accounting, "record_lease_success", AsyncMock())
    sent = []
    reply = {"status": 200, "body": message(), "headers": {}}

    async def upstream(request):
        sent.append(request)
        if isinstance(reply["body"], (dict, list)):
            return httpx.Response(reply["status"], json=reply["body"], headers=reply["headers"])
        return httpx.Response(reply["status"], content=reply["body"], headers=reply["headers"])

    monkeypatch.setattr(endpoint, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))

    async def call(*, stream=False, payload=None, path="/v1/messages"):
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            return await client.post(path, json=payload or {"model": "model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100, "stream": stream})

    env.app, env.call, env.reply, env.sent = app, call, reply, sent
    return env


def records(api, success):
    logs = [row for row in api.db.added if isinstance(row, RequestLog)]
    attempts = [row for row in api.db.added if isinstance(row, RequestAttempt)]
    ledgers = [row for row in api.db.added if isinstance(row, UsageLedger)]
    assert len(logs) == len(attempts) == len(ledgers) == 1
    assert logs[0].success == success
    assert attempts[0].outcome == ("success" if success else "failed")
    assert ledgers[0].status == ("success" if success else "failed")
    assert api.engine.adaptive._stats[("model", 1)].inflight == 0
    if not success:
        api.accounting.record_lease_success.assert_not_awaited()
    return attempts[0]


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("body", [
    {"code": 500, "msg": "404 NOT_FOUND", "success": False},
    {"error": {"type": "invalid_request_error", "message": "bad provider-key"}},
    {}, [], {"type": "message", "content": []},
])
def test_http200_invalid_body_cannot_be_nonstream_success(api, native, body):
    api.channels[0].protocol = "anthropic" if native else "openai"
    api.channels[0].type = "openai"
    api.reply["body"] = body
    result = asyncio.run(api.call())
    assert result.status_code == 502
    assert result.json()["type"] == "error"
    assert result.json()["error"]["type"] == "api_error"
    assert "provider-key" not in result.text
    attempt = records(api, False)
    assert attempt.upstream_status == 200
    assert result.json()["request_id"] == attempt.request_id


@pytest.mark.parametrize("failure", ["json-body", "empty", "bad-json", "error", "conflicting-event", "missing-start", "missing-stop", "unclosed-block", "truncated-tool"])
def test_native_stream_invalid_or_truncated_never_succeeds(api, failure):
    events = native_events()
    body = wire(events)
    if failure == "json-body":
        api.reply["body"] = {"code": 500, "msg": "404 NOT_FOUND", "success": False}
    else:
        api.reply["headers"] = {"content-type": "text/event-stream"}
        if failure == "empty":
            body = ""
        elif failure == "bad-json":
            body = "data: {bad json}\n\n"
        elif failure == "error":
            body = wire([{"type": "error", "error": {"type": "invalid_request_error", "message": "bad provider-key"}}])
        elif failure == "conflicting-event":
            body = 'event: error\ndata: {"type":"ping"}\n\n'
        elif failure == "missing-start":
            body = wire(events[1:])
        elif failure == "missing-stop":
            body = wire(events[:-1])
        elif failure in {"unclosed-block", "truncated-tool"}:
            content = [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"}},
            ]
            if failure == "truncated-tool":
                content.append({"type": "content_block_stop", "index": 0})
            body = wire([events[0], *content, *events[1:]])
        api.reply["body"] = body
    result = asyncio.run(api.call(stream=True))
    assert result.status_code == 200
    assert "event: error\n" in result.text and "provider-key" not in result.text
    assert "event: message_stop\n" not in result.text
    assert records(api, False).upstream_status == 200
    assert len(api.sent) == 1


@pytest.mark.parametrize("failure", ["empty", "error", "missing-stop", "truncated-tool"])
def test_converted_stream_requires_real_terminal_and_valid_tools(api, failure):
    api.channels[0].protocol = "openai"
    api.channels[0].type = "openai"
    chunks = [{"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}]
    if failure == "empty":
        chunks = []
    elif failure == "error":
        chunks.append({"error": {"message": "bad provider-key"}})
    elif failure == "truncated-tool":
        chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": "{"}}]}, "finish_reason": "tool_calls"}]}]
    api.reply.update(body=wire(chunks), headers={"content-type": "text/event-stream"})
    result = asyncio.run(api.call(stream=True))
    assert "event: error\n" in result.text and "provider-key" not in result.text
    assert "event: message_stop\n" not in result.text
    assert records(api, False).upstream_status == 200
    assert len(api.sent) == 1


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_explicit_empty_content_terminal_is_valid(api, native, stream):
    api.channels[0].protocol = "anthropic" if native else "openai"
    api.channels[0].type = "openai"
    if native:
        body = wire(native_events()) if stream else message()
    else:
        body = wire([{"choices": [{"delta": {}, "finish_reason": "stop"}]}]) if stream else {
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        }
    api.reply.update(body=body, headers={"content-type": "text/event-stream"} if stream else {})
    result = asyncio.run(api.call(stream=stream))
    assert result.status_code == 200
    assert "event: error\n" not in result.text
    records(api, True)


def test_native_multiline_sse_archive_reconstructs_complete_message(api):
    events = native_events()
    events[0]["message"]["usage"]["input_tokens"] = 3
    events[1]["delta"]["stop_reason"] = "tool_use"
    events[1]["usage"] = {"output_tokens": 7, "cache_read_input_tokens": 2}
    blocks = [
        ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "hello "}, {"type": "text_delta", "text": "world"}]),
        ({"type": "thinking", "thinking": "", "signature": ""}, [{"type": "thinking_delta", "thinking": "reason"}, {"type": "signature_delta", "signature": "signed"}]),
        ({"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}}, [{"type": "input_json_delta", "partial_json": "{\"city\":"}, {"type": "input_json_delta", "partial_json": "\"Paris\"}"}]),
        ({"type": "vendor_block", "opaque": {"preserve": True}}, []),
    ]
    contents = []
    for index, (block, deltas) in enumerate(blocks):
        contents.append({"type": "content_block_start", "index": index, "content_block": block})
        contents.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        contents.append({"type": "content_block_stop", "index": index})
    extension = {"type": "vendor_progress", "value": "preserve"}
    all_events = [events[0], extension, *contents, *events[1:]]
    api.reply.update(body=wire(all_events, multiline=True), headers={"content-type": "text/event-stream"})
    result = asyncio.run(api.call(stream=True))
    client_events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    assert client_events == all_events
    archived = endpoint.conversation_store.append_response.await_args.args[1]
    assert archived["id"] == "msg_actual" and archived["stop_reason"] == "tool_use"
    assert archived["usage"] == {"input_tokens": 3, "output_tokens": 7, "cache_read_input_tokens": 2}
    assert archived["content"] == [
        {"type": "text", "text": "hello world"},
        {"type": "thinking", "thinking": "reason", "signature": "signed"},
        {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {"city": "Paris"}},
        {"type": "vendor_block", "opaque": {"preserve": True}},
    ]
    assert archived["stream_extensions"] == [extension]
    records(api, True)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_tool_only_completion_with_empty_object_input_is_valid(api, native, stream):
    api.channels[0].protocol = "anthropic" if native else "openai"
    api.channels[0].type = "openai"
    tool = {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}}
    if native:
        if stream:
            events = native_events()
            events[1]["delta"]["stop_reason"] = "tool_use"
            body = wire([events[0], {"type": "content_block_start", "index": 0, "content_block": tool},
                         {"type": "content_block_stop", "index": 0}, *events[1:]])
        else:
            body = message(content=[tool], stop_reason="tool_use")
    else:
        call = {"id": "tool_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        body = wire([{"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]}, "finish_reason": "tool_calls"}]}]) if stream else {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}],
        }
    api.reply.update(body=body, headers={"content-type": "text/event-stream"} if stream else {})
    result = asyncio.run(api.call(stream=stream))
    assert result.status_code == 200 and "event: error\n" not in result.text
    if not stream:
        assert result.json()["content"] == [tool]
    records(api, True)


@pytest.mark.parametrize("invalid", ["missing-id", "missing-name", "reasoning", "refusal", "finish"])
def test_converted_nonstream_failure_keeps_actual_upstream_status(api, invalid):
    api.channels[0].type = api.channels[0].protocol = "openai"
    completion = {"role": "assistant", "content": ""}
    finish = "stop"
    if invalid in {"missing-id", "missing-name"}:
        call = {"id": "tool_1", "function": {"name": "lookup", "arguments": "{}"}}
        if invalid == "missing-id":
            del call["id"]
        else:
            del call["function"]["name"]
        completion["tool_calls"] = [call]
        finish = "tool_calls"
    elif invalid == "finish":
        finish = "content_filter"
    else:
        completion["reasoning_content" if invalid == "reasoning" else "refusal"] = "unmappable"
    api.reply["body"] = {"choices": [{"message": completion, "finish_reason": finish}]}
    result = asyncio.run(api.call())
    assert result.status_code == 502 and result.json()["type"] == "error"
    assert records(api, False).upstream_status == 200


def test_partial_failure_never_switches_channel_or_recovers_probe(api):
    api.engine._clock = api.clock.monotonic
    api.engine.mark_unavailable("model", api.channels[0], 0)
    alternate = deepcopy(api.channels[0])
    alternate.id = 2
    alternate.base_url = "https://alternate.invalid/v1"
    api.channels.append(alternate)
    events = native_events()
    api.reply.update(body=wire([events[0],
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}},
        {"type": "error", "error": {"type": "api_error", "message": "failed"}},
    ]), headers={"content-type": "text/event-stream"})
    result = asyncio.run(api.call(stream=True))
    assert "partial" in result.text and "event: error\n" in result.text
    records(api, False)
    assert len(api.sent) == 1
    assert api.engine._cooldowns[("model", 1)].probe is None
    assert api.engine._is_cooling_down("model", 1)


@pytest.mark.parametrize("kind,status,expected_type", [
    ("authentication", 401, "authentication_error"), ("validation", 422, "invalid_request_error"),
    ("http", 403, "permission_error"), ("temporary", 503, "overloaded_error"),
    ("generic", 500, "api_error"), ("upstream", 429, "rate_limit_error"),
    ("http-billing", 402, "billing_error"), ("http-conflict", 409, "conflict_error"),
    ("http-timeout", 504, "timeout_error"),
])
def test_actual_asgi_error_handlers_use_safe_anthropic_envelopes(api, kind, status, expected_type):
    if kind == "authentication":
        def denied():
            raise AuthenticationException("Invalid credentials")
        api.app.dependency_overrides[get_current_token] = denied
    elif kind in {"http", "temporary", "generic", "http-billing", "http-conflict", "http-timeout"}:
        def denied():
            if kind.startswith("http"):
                raise HTTPException(status, "Request failed", headers={"Retry-After": "7"})
            if kind == "temporary":
                raise ChannelsTemporarilyUnavailable("model", 7)
            raise RuntimeError("provider-key and confidential input")
        api.app.dependency_overrides[get_current_token] = denied
    elif kind == "upstream":
        api.reply.update(status=429, body={"error": {"message": "provider-key", "x-api-key": "provider-key"}}, headers={"Retry-After": "7"})
    payload = {"model": "model", "secret": "confidential input"} if kind == "validation" else None
    result = asyncio.run(api.call(payload=payload, path="/anthropic/v1/messages"))
    assert result.status_code == status
    assert result.json()["type"] == "error"
    assert result.json()["error"]["type"] == expected_type
    assert result.json()["request_id"]
    assert "provider-key" not in result.text and "confidential input" not in result.text
    if kind in {"http", "temporary", "upstream"}:
        assert result.headers["retry-after"] == "7"


def test_other_paths_keep_fastapi_http_and_validation_shapes(api):
    @api.app.get("/other/http")
    async def other_http():
        raise HTTPException(400, "original detail")

    @api.app.get("/other/validation")
    async def other_validation(number: int):
        return number

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
            bad = await client.get("/other/http")
            invalid = await client.get("/other/validation?number=bad")
            assert bad.status_code == 400 and bad.json() == {"detail": "original detail"}
            assert invalid.status_code == 422 and "detail" in invalid.json()

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["openai", "kimi", "minimax", "moonshot"])
@pytest.mark.parametrize("shape", ["malformed-then-finish", "no-space", "multiline"])
def test_converted_provider_raw_sse_is_strict_only_for_anthropic(api, provider, shape):
    api.channels[0].type = provider
    api.channels[0].protocol = "openai"
    chunk = {"choices": [{"delta": {"content": "retained"}, "finish_reason": "stop"}]}
    if shape == "malformed-then-finish":
        body = "data: {invalid}\n\n" + wire([chunk])
    elif shape == "no-space":
        body = "data:" + json.dumps(chunk) + "\n\n"
    else:
        body = ": comment\n" + "\n".join("data:" + line for line in json.dumps(chunk, indent=2).splitlines()) + "\n\n"
    body += "data:[DONE]\n\n"
    api.reply.update(body=body, headers={"content-type": "text/event-stream"})
    result = asyncio.run(api.call(stream=True))
    if shape == "malformed-then-finish":
        assert "event: error\n" in result.text
        assert "event: message_stop\n" not in result.text
        assert records(api, False).upstream_status == 200
    else:
        assert "retained" in result.text and "event: message_stop\n" in result.text
        assert "event: error\n" not in result.text
        records(api, True)
    assert len(api.sent) == 1


@pytest.mark.parametrize("body", [{}, {"code": 500, "msg": "404 NOT_FOUND", "success": False}, {"error": {"message": "bad"}}])
def test_kimi_does_not_wrap_bad_raw_body_as_empty_completion(api, body):
    api.channels[0].type = "kimi"
    api.channels[0].protocol = "openai"
    api.reply["body"] = body
    result = asyncio.run(api.call())
    assert result.status_code == 502 and result.json()["type"] == "error"
    assert records(api, False).upstream_status == 200
