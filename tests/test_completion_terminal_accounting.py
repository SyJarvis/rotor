import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.core.deps import get_current_token
from rotor.core.exceptions import ChannelException
from rotor.database import Base, get_db
from rotor.gateway.accounting import AccountingService
from rotor.main import app as configured_app
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.request_attempt import RequestAttempt
from rotor.models.response_route import ResponseRoute
from rotor.models.token import Token
from rotor.models.usage import UsageLedger
from tests.test_attempt_latency import environment


def native_response(status="completed", usage=True):
    payload = {
        "id": "resp_upstream", "object": "response", "created_at": 1,
        "model": "model", "status": status, "error": None,
        "output": [{"id": "msg_upstream", "type": "message", "role": "assistant",
                    "status": status, "content": [{"type": "output_text", "text": "partial", "annotations": []}]}],
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10} if usage else None,
    }
    if status == "incomplete":
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
    if status in {"queued", "in_progress", "failed", "cancelled"}:
        payload["output"] = []
    if status == "failed":
        payload["error"] = {"code": "server_error", "message": "Provider failure"}
    return payload


def chat_chunk(delta, finish=None, *, usage=False, index=0):
    payload = {"id": "chat_upstream", "object": "chat.completion.chunk", "model": "model",
               "choices": [{"index": index, "delta": delta, "finish_reason": finish}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    return payload


@pytest.fixture
def harness(environment, monkeypatch):
    env = environment
    env.channels[:] = env.channels[:1]
    env.engine._clock = env.clock.monotonic
    env.engine.mark_unavailable("model", env.channels[0], 0)
    lease = AsyncMock()
    monkeypatch.setattr(env.accounting, "record_lease_success", lease)
    counters = AsyncMock(wraps=env.accounting._update_token_counters)
    monkeypatch.setattr(env.accounting, "_update_token_counters", counters)
    route_saves = AsyncMock()
    monkeypatch.setattr(responses_endpoint, "save_response_route", route_saves)
    state = SimpleNamespace(body=None, replies=None, events=None, protocol="openai", requests=[], clients=[], responses=[])

    async def upstream(request):
        state.requests.append(request.url.path)
        if state.events is None:
            payload = state.replies[len(state.requests) - 1] if state.replies else state.body
            response = httpx.Response(200, json=payload)
        else:
            content = "".join(f"data: {json.dumps(item)}\n\n" for item in state.events)
            response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())
        state.responses.append(response)
        return response

    def client_factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)
        state.clients.append(client)
        return client

    for module in (chat_endpoint, responses_endpoint):
        monkeypatch.setattr(module, "AsyncClient", client_factory)
    app = FastAPI()
    app.exception_handlers.update(configured_app.exception_handlers)
    app.include_router(chat_endpoint.router, prefix="/v1")
    app.include_router(responses_endpoint.router, prefix="/v1")

    async def db_override():
        yield env.db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_token] = lambda: env.token

    async def call(client_protocol, **fields):
        env.channels[0].protocol = state.protocol
        body = {"model": "model", **fields}
        if client_protocol == "chat":
            path = "/v1/chat/completions"
            body["messages"] = [{"role": "user", "content": "hi"}]
        else:
            path = "/v1/responses"
            body["input"] = "hi"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Rotor-Session-Id": "test-session"})

    return SimpleNamespace(env=env, state=state, call=call, lease=lease, counters=counters, route_saves=route_saves)


def assert_accounting(harness, *, success, tokens=10):
    env = harness.env
    logs = [row for row in env.db.added if isinstance(row, RequestLog)]
    attempts = [row for row in env.db.added if isinstance(row, RequestAttempt)]
    ledgers = [row for row in env.db.added if isinstance(row, UsageLedger)]
    assert len(logs) == len(attempts) == len(ledgers) == 1
    assert bool(logs[0].success) is success
    assert logs[0].total_tokens == tokens
    assert ledgers[0].status == ("success" if success else "failed")
    assert ledgers[0].total_tokens == tokens
    assert attempts[0].outcome == ("success" if success else "failed")
    if not success:
        assert attempts[0].upstream_status == 200
        assert ledgers[0].cost_status == "not_applicable"
    assert harness.lease.await_count == int(success)
    assert harness.counters.await_count == int(success)
    stats = env.engine.adaptive._stats[("model", 1)]
    assert stats.inflight == 0
    assert stats.successes == int(success)
    assert (("model", 1) not in env.engine._cooldowns) is success
    assert len(harness.state.requests) == 1
    assert all(response.is_closed for response in harness.state.responses)
    assert all(client.is_closed for client in harness.state.clients)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
@pytest.mark.parametrize("provider_protocol", ["openai", "responses"])
def test_business_error_never_records_success(harness, client_protocol, provider_protocol):
    harness.state.protocol = provider_protocol
    harness.state.body = {"code": 500, "success": False, "msg": "404 NOT_FOUND", "usage": {
        "input_tokens": 7, "output_tokens": 3, "prompt_tokens": 7, "completion_tokens": 3,
    }}
    response = asyncio.run(harness.call(client_protocol))
    assert response.status_code == 502
    assert_accounting(harness, success=False)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
def test_native_failed_result_keeps_failure_and_known_usage(harness, client_protocol):
    harness.state.protocol = "responses"
    harness.state.body = native_response("failed")
    response = asyncio.run(harness.call(client_protocol))
    assert response.status_code == 502
    assert_accounting(harness, success=False)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
@pytest.mark.parametrize("provider_protocol", ["openai", "responses"])
def test_http_200_overload_keeps_nonstream_fallback(harness, client_protocol, provider_protocol):
    harness.state.protocol = provider_protocol
    first = harness.env.channels[0]
    first.protocol = provider_protocol
    second_fields = {**vars(first), "id": 2, "name": "fallback"}
    harness.env.channels.append(SimpleNamespace(**second_fields))
    failed = {"error": {"type": "overloaded_error", "message": "overloaded"},
              "usage": {"input_tokens": 7, "output_tokens": 3, "prompt_tokens": 7, "completion_tokens": 3}}
    completed = (native_response() if provider_protocol == "responses" else {
        "id": "chat_1", "model": "model", "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    })
    harness.state.replies = [failed, completed]
    response = asyncio.run(harness.call(client_protocol))
    assert response.status_code == 200
    attempts = [row for row in harness.env.db.added if isinstance(row, RequestAttempt)]
    assert [attempt.outcome for attempt in attempts] == ["failed", "success"]
    assert attempts[0].upstream_status == 200
    assert attempts[0].fallback_allowed
    ledgers = [row for row in harness.env.db.added if isinstance(row, UsageLedger)]
    assert [(row.status, row.total_tokens) for row in ledgers] == [("failed", 10), ("success", 10)]
    assert harness.lease.await_count == harness.counters.await_count == 1
    assert len(harness.state.requests) == 2
    assert all(stats.inflight == 0 for stats in harness.env.engine.adaptive._stats.values())


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
@pytest.mark.parametrize("failure", ["eof", "error"])
def test_converted_partial_stream_never_gets_success_terminal(harness, client_protocol, failure):
    harness.state.events = [chat_chunk({"content": "partial"}, usage=True)]
    if failure == "error":
        harness.state.events.append({"error": {"type": "invalid_request_error", "message": "rejected"}})
    response = asyncio.run(harness.call(client_protocol, stream=True))
    assert response.status_code == 200
    assert "partial" in response.text
    assert "[DONE]" not in response.text
    assert "response.completed" not in response.text
    assert "response.output_text.done" not in response.text
    assert "error" in response.text
    assert_accounting(harness, success=False)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
def test_native_failed_stream_keeps_known_usage(harness, client_protocol):
    harness.state.protocol = "responses"
    harness.state.events = [
        {"type": "response.created", "sequence_number": 0, "response": native_response("in_progress", usage=False)},
        {"type": "response.output_text.delta", "sequence_number": 1, "output_index": 0, "delta": "partial"},
        {"type": "response.failed", "sequence_number": 2, "response": native_response("failed")},
    ]
    response = asyncio.run(harness.call(client_protocol, stream=True))
    assert "partial" in response.text
    assert "[DONE]" not in response.text
    assert "response.completed" not in response.text
    assert_accounting(harness, success=False)


@pytest.mark.parametrize("provider_protocol", ["openai", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_incomplete_is_valid_and_preserves_output_and_usage(harness, provider_protocol, stream, reason):
    harness.state.protocol = provider_protocol
    if provider_protocol == "responses":
        payload = native_response("incomplete")
        payload["incomplete_details"] = {"reason": reason}
        harness.state.body = payload
        if stream:
            harness.state.events = [
                {"type": "response.created", "sequence_number": 0, "response": native_response("in_progress", usage=False)},
                {"type": "response.output_text.delta", "sequence_number": 1, "output_index": 0, "delta": "partial"},
                {"type": "response.incomplete", "sequence_number": 2, "response": payload},
            ]
    else:
        harness.state.body = {"id": "chat_upstream", "model": "model", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "partial"},
             "finish_reason": "length" if reason == "max_output_tokens" else "content_filter"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        if stream:
            harness.state.events = [chat_chunk({"content": "partial"}), chat_chunk({}, "length" if reason == "max_output_tokens" else "content_filter", usage=True)]
    response = asyncio.run(harness.call("responses", stream=stream))
    assert response.status_code == 200
    result = ([json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]["response"]
              if stream else response.json())
    assert result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": reason}
    assert result["usage"]["total_tokens"] == 10
    assert result["output"][0]["content"][0]["text"] == "partial"
    if provider_protocol == "responses":
        assert result["id"] == "resp_upstream"
        archive = chat_endpoint.conversation_store.append_response.await_args.args[1]
        assert archive == result
    assert_accounting(harness, success=True)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
def test_native_partial_eof_has_no_success_terminal(harness, client_protocol):
    harness.state.protocol = "responses"
    harness.state.events = [
        {"type": "response.created", "sequence_number": 0, "response": native_response("in_progress", usage=False)},
        {"type": "response.output_text.delta", "sequence_number": 1, "output_index": 0, "delta": "partial"},
    ]
    response = asyncio.run(harness.call(client_protocol, stream=True))
    assert "partial" in response.text
    assert "[DONE]" not in response.text and "response.completed" not in response.text
    if client_protocol == "responses":
        final = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]
        assert final["response"]["id"] == "resp_upstream"
        assert final["sequence_number"] == 2
    assert_accounting(harness, success=False, tokens=0)


@pytest.mark.parametrize("complete", [True, False])
def test_all_requested_chat_choices_require_terminal(harness, complete):
    harness.state.events = [
        {"choices": [{"index": 0, "delta": {"content": "first"}, "finish_reason": None},
                     {"index": 1, "delta": {"content": "second"}, "finish_reason": None}]},
        chat_chunk({}, "stop", index=0, usage=True),
    ]
    if complete:
        harness.state.events.append(chat_chunk({}, "stop", index=1))
    response = asyncio.run(harness.call("chat", stream=True, n=2))
    assert "first" in response.text and "second" in response.text
    assert ("[DONE]" in response.text) is complete
    assert_accounting(harness, success=complete)


@pytest.mark.parametrize("complete", [True, False])
def test_nonstream_retains_and_validates_all_requested_choices(harness, complete):
    choices = [{"index": index, "message": {"role": "assistant", "content": f"answer-{index}"}, "finish_reason": "stop"}
               for index in range(2 if complete else 1)]
    harness.state.body = {"id": "chat_1", "model": "model", "choices": choices,
                          "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    response = asyncio.run(harness.call("chat", n=2))
    assert response.status_code == (200 if complete else 502)
    if complete:
        assert response.json()["choices"] == choices
    assert_accounting(harness, success=complete)


@pytest.mark.parametrize("client_protocol", ["chat", "responses"])
def test_invalid_tool_arguments_remain_a_model_output(harness, client_protocol):
    harness.state.events = [
        chat_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "lookup", "arguments": '{"city":'}}]}),
        chat_chunk({}, "tool_calls", usage=True),
    ]
    response = asyncio.run(harness.call(client_protocol, stream=True))
    assert response.status_code == 200
    assert "lookup" in response.text
    assert_accounting(harness, success=True)


@pytest.mark.parametrize("has_usage", [True, False])
def test_converted_failure_is_validated_before_nonstream_success(harness, has_usage):
    harness.state.body = {"id": "chat_1", "model": "model", "choices": [
        {"message": {"role": "assistant", "content": None, "reasoning_details": [{"text": "private"}]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3} if has_usage else None}
    response = asyncio.run(harness.call("responses"))
    assert response.status_code == 502
    assert_accounting(harness, success=False, tokens=10 if has_usage else 0)
    ledger = next(row for row in harness.env.db.added if isinstance(row, UsageLedger))
    assert ledger.usage_source == ("provider" if has_usage else "missing")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("usage_kind", ["absent", "null", "empty", "zero"])
def test_converted_wire_does_not_invent_zero_usage(harness, stream, usage_kind):
    usage = {"prompt_tokens": 0, "completion_tokens": 0} if usage_kind == "zero" else {} if usage_kind == "empty" else None
    if stream:
        harness.state.events = [chat_chunk({"content": "ok"}), chat_chunk({}, "stop")]
        payload = harness.state.events[-1]
    else:
        harness.state.body = {"id": "chat_1", "model": "model", "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        payload = harness.state.body
    if usage_kind != "absent":
        payload["usage"] = usage
    response = asyncio.run(harness.call("responses", stream=stream))
    assert response.status_code == 200
    result = ([json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]["response"]
              if stream else response.json())
    if usage_kind == "zero":
        assert result["usage"]["total_tokens"] == 0
    else:
        assert result["usage"] is None
    assert_accounting(harness, success=True, tokens=0)
    ledger = next(row for row in harness.env.db.added if isinstance(row, UsageLedger))
    assert ledger.usage_source == ("provider" if usage_kind == "zero" else "missing")


@pytest.mark.parametrize("status", ["queued", "in_progress"])
@pytest.mark.parametrize("has_usage", [True, False])
def test_background_acceptance_does_not_finalize_usage(harness, status, has_usage):
    harness.state.protocol = "responses"
    harness.state.body = native_response(status, usage=has_usage)
    response = asyncio.run(harness.call("responses", background=True))
    assert response.status_code == 200
    assert response.json() == harness.state.body
    assert not [row for row in harness.env.db.added if isinstance(row, (UsageLedger, RequestLog))]
    attempts = [row for row in harness.env.db.added if isinstance(row, RequestAttempt)]
    assert len(attempts) == 1 and attempts[0].outcome == "success"
    assert harness.lease.await_count == 1
    assert harness.counters.await_count == 0
    assert harness.env.engine.adaptive._stats[("model", 1)].inflight == 0
    assert harness.route_saves.await_count == 1
    assert harness.route_saves.await_args.kwargs["usage_accounted"] is False


def test_deferred_invalid_completed_payload_cannot_claim_usage(harness):
    route = SimpleNamespace(id=1, usage_accounted=False, model="model", conversation_id="conv")
    payload = native_response()
    payload["error"] = {"code": "server_error", "message": "failure"}
    with pytest.raises(ChannelException) as error:
        asyncio.run(responses_endpoint._account_deferred_response_usage(
            harness.env.db, route=route, channel=harness.env.channels[0], token=harness.env.token,
            payload=payload, client_ip="127.0.0.1", latency_ms=1,
        ))
    assert error.value.status_code == 502
    assert not route.usage_accounted
    assert not [row for row in harness.env.db.added if isinstance(row, (RequestLog, UsageLedger))]


@pytest.mark.parametrize("status,has_usage,expected_count", [
    ("failed", True, 1), ("cancelled", True, 1),
    ("queued", True, 0), ("in_progress", True, 0), ("failed", False, 0),
])
def test_failed_background_usage_is_once_and_never_charged(monkeypatch, status, has_usage, expected_count):
    service = AccountingService()
    monkeypatch.setattr(responses_endpoint, "accounting_service", service)

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                token = Token(key="local-test", name="local", quota=100)
                channel = Channel(name="local", type="openai", protocol="responses", key="unused",
                                  base_url="https://unused.invalid", model_mapping={}, extra={})
                db.add_all([token, channel])
                await db.flush()
                route = ResponseRoute(response_id="resp_upstream", token_id=token.id, channel_id=channel.id,
                                      model="model", status=status, usage_accounted=False)
                db.add(route)
                await db.commit()
                for _ in range(2):
                    await responses_endpoint._account_deferred_response_usage(
                        db, route=route, channel=channel, token=token,
                        payload=native_response(status, usage=has_usage), client_ip="127.0.0.1", latency_ms=1,
                    )
                    await db.commit()
                await db.refresh(token)
                assert (token.request_count, token.token_count, token.used_quota) == (0, 0, 0)
                assert route.usage_accounted is bool(expected_count)
                logs = list((await db.scalars(select(RequestLog))).all())
                ledgers = list((await db.scalars(select(UsageLedger))).all())
                assert len(logs) == len(ledgers) == expected_count
                if expected_count:
                    assert not logs[0].success and logs[0].total_tokens == 10
                    assert ledgers[0].status == "failed" and ledgers[0].total_tokens == 10
                    assert ledgers[0].usage_source == "provider"
                    assert ledgers[0].cost_status == "not_applicable"
                    assert ledgers[0].total_cost == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())
