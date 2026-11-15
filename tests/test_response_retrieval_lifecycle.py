import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request

from rotor.api.v1 import responses as endpoint
from rotor.core.exceptions import ChannelException
from rotor.gateway.routing import RoutingEngine
from rotor.gateway.streaming import AdmissionStreamingResponse


SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}


async def receive():
    await asyncio.Event().wait()


@pytest.fixture
def env(monkeypatch):
    engine = RoutingEngine()
    channel = SimpleNamespace(id=1, enabled=True, extra={})
    route = SimpleNamespace(model="test-model", status="queued", usage_accounted=False)
    older = engine.admit_attempt(route.model, channel)
    upstream = SimpleNamespace(
        headers={"content-type": "application/json"}, status_code=200,
        content=b'{"id":"resp_1","status":"queued"}',
        json=lambda: {"id": "resp_1", "status": "queued"},
        aclose=AsyncMock(),
    )
    client = SimpleNamespace(aclose=AsyncMock())
    state = SimpleNamespace(
        engine=engine, channel=channel, route=route, older=older,
        upstream=upstream, client=client, client_created=False,
        db=SimpleNamespace(close=AsyncMock(), commit=AsyncMock()),
        token=SimpleNamespace(id=7), chunks=[b"data: [DONE]\n\n"],
        body_error=None, body_started=False, admissions=[],
        retrieve=AsyncMock(return_value=upstream),
        account=AsyncMock(),
    )

    async def body():
        state.body_started = True
        for chunk in state.chunks:
            yield chunk
        if state.body_error is not None:
            raise state.body_error

    upstream.aiter_raw = body

    class Adapter(endpoint.OpenAIResponsesAdapter):
        def __init__(self):
            pass

        async def retrieve_response(self, *args, **kwargs):
            return await state.retrieve(*args, **kwargs)

    def create_client(**kwargs):
        state.client_created = True
        return client

    admit = engine.admit_attempt

    def track_admission(*args):
        admission = admit(*args)
        state.admissions.append(admission)
        return admission

    monkeypatch.setattr(engine, "admit_attempt", track_admission)
    monkeypatch.setattr(endpoint, "routing_engine", engine)
    monkeypatch.setattr(endpoint, "_resolve_response_channel", AsyncMock(return_value=(route, channel)))
    monkeypatch.setattr(endpoint, "AsyncClient", create_client)
    monkeypatch.setattr(endpoint.AdapterFactory, "create_adapter", lambda *args: Adapter())
    monkeypatch.setattr(endpoint, "_account_deferred_response_usage", state.account)

    async def retrieve(stream=True):
        request = Request({
            "type": "http", "query_string": b"stream=true&include=x&include=y" if stream else b"",
            "client": ("127.0.0.1", 123),
        })
        return await endpoint.retrieve_response("resp_1", request, state.db, state.token)

    state.call = retrieve
    return state


def assert_released(env):
    assert len(env.admissions) == 1
    admission = env.admissions[0]
    assert admission.released
    assert not env.older.released
    assert env.engine.adaptive._stats[(env.route.model, env.channel.id)].inflight == 1
    env.engine.release_attempt(admission)
    assert env.engine.adaptive._stats[(env.route.model, env.channel.id)].inflight == 1


@pytest.mark.parametrize("failure", ["db_error", "db_cancel", "client", "adapter", "wrong_adapter"])
def test_failure_before_upstream_releases_admission(env, monkeypatch, failure):
    error = asyncio.CancelledError("db cancelled") if failure == "db_cancel" else RuntimeError(failure)
    if failure.startswith("db_"):
        env.db.close.side_effect = error
    elif failure == "client":
        def broken_client(**kwargs):
            raise error
        monkeypatch.setattr(endpoint, "AsyncClient", broken_client)
    elif failure == "adapter":
        def broken_adapter(*args):
            raise error
        monkeypatch.setattr(endpoint.AdapterFactory, "create_adapter", broken_adapter)
    else:
        monkeypatch.setattr(endpoint.AdapterFactory, "create_adapter", lambda *args: object())

    async def scenario():
        expected = HTTPException if failure == "wrong_adapter" else type(error)
        with pytest.raises(expected) as captured:
            await env.call()
        if failure == "wrong_adapter":
            assert captured.value.status_code == 404
        else:
            assert captured.value is error
        assert_released(env)
        env.retrieve.assert_not_awaited()
        assert env.client.aclose.await_count == int(env.client_created)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("close_error", [None, RuntimeError, asyncio.CancelledError])
def test_retrieve_cancellation_preserved_even_when_client_close_fails(env, stream, close_error):
    original = asyncio.CancelledError("upstream cancelled")
    env.retrieve.side_effect = original
    if close_error:
        env.client.aclose.side_effect = close_error("close failed")

    async def scenario():
        with pytest.raises(asyncio.CancelledError) as captured:
            await env.call(stream)
        assert captured.value is original
        assert_released(env)
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("close_error", [None, RuntimeError, asyncio.CancelledError])
def test_normal_completion_and_close_failure_do_not_skip_remaining_cleanup(env, stream, close_error):
    if close_error:
        env.upstream.aclose.side_effect = close_error("upstream close failed")

    async def scenario():
        response = await env.call(stream)
        if stream:
            assert isinstance(response, AdmissionStreamingResponse)
            assert not env.admissions[0].provider_succeeded
            assert not env.admissions[0].released
            assert [chunk async for chunk in response.body_iterator] == env.chunks
            env.db.close.assert_awaited_once()
            env.account.assert_not_awaited()
        else:
            assert response.status_code == 200
            assert response.body == env.upstream.content
            env.db.close.assert_not_awaited()
            env.account.assert_awaited_once()
            env.db.commit.assert_awaited_once()
        assert env.admissions[0].provider_succeeded
        assert_released(env)
        env.upstream.aclose.assert_awaited_once()
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, asyncio.CancelledError])
def test_stream_body_failure_releases_and_marks_only_retryable_failures(env, error_type):
    error = error_type("body failed")
    env.body_error = error
    env.upstream.aclose.side_effect = RuntimeError("close failed")

    async def scenario():
        response = await env.call()
        with pytest.raises(error_type) as captured:
            async for _ in response.body_iterator:
                pass
        assert captured.value is error
        assert not env.admissions[0].provider_succeeded
        assert_released(env)
        assert bool(env.engine.routing_state()) == (error_type is httpx.ReadTimeout)
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError, OSError])
def test_asgi_start_failure_closes_unstarted_relay(env, error_type):
    original = error_type("start failed")
    env.upstream.aclose.side_effect = asyncio.CancelledError("close cancelled")

    async def scenario():
        response = await env.call()

        async def send(message):
            raise original

        expected = ClientDisconnect if error_type is OSError else error_type
        with pytest.raises(expected) as captured:
            await response(SCOPE, receive, send)
        if error_type is not OSError:
            assert captured.value is original
        assert not env.body_started
        assert not env.admissions[0].provider_succeeded
        assert_released(env)
        env.upstream.aclose.assert_awaited_once()
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


def test_asgi_disconnect_closes_suspended_relay(env):
    async def scenario():
        response = await env.call()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("disconnected")

        with pytest.raises(ClientDisconnect):
            await response(SCOPE, receive, send)
        assert env.body_started
        assert not env.admissions[0].provider_succeeded
        assert_released(env)
        env.upstream.aclose.assert_awaited_once()
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


def test_legacy_asgi_disconnect_cleans_up_unstarted_relay(env):
    async def scenario():
        response = await env.call()

        async def disconnected():
            return {"type": "http.disconnect"}

        async def send(message):
            await anyio.sleep(0)

        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, disconnected, send)
        assert not env.body_started
        assert_released(env)
        env.upstream.aclose.assert_awaited_once()
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


def test_task_cancel_during_db_close_releases_admission(env):
    async def scenario():
        started = asyncio.Event()

        async def close_db():
            started.set()
            await asyncio.Event().wait()

        env.db.close.side_effect = close_db
        task = asyncio.create_task(env.call())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel("db disconnect")
        with pytest.raises(asyncio.CancelledError, match="db disconnect"):
            await task
        assert_released(env)
        assert not env.client_created

    asyncio.run(scenario())


def test_anyio_cancel_scope_still_closes_client_and_releases_admission(env):
    async def scenario():
        async def retrieve(*args, **kwargs):
            await anyio.sleep(0)

        async def close_client():
            await anyio.sleep(0)

        env.retrieve.side_effect = retrieve
        env.client.aclose.side_effect = close_client
        with anyio.CancelScope() as scope:
            scope.cancel()
            await env.call(stream=False)
        assert_released(env)
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["json", "account", "commit"])
def test_nonstream_processing_failure_preserves_error_and_cleans_up(env, failure):
    original = RuntimeError(failure)
    if failure == "json":
        def invalid_json():
            raise original
        env.upstream.json = invalid_json
    elif failure == "account":
        env.account.side_effect = original
    else:
        env.db.commit.side_effect = original
    env.upstream.aclose.side_effect = RuntimeError("close failed")

    async def scenario():
        with pytest.raises(RuntimeError) as captured:
            await env.call(stream=False)
        assert captured.value is original
        assert_released(env)
        env.upstream.aclose.assert_awaited_once()
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


def test_repeated_task_cancel_waits_for_cleanup_without_masking_original(env):
    async def scenario():
        started = asyncio.Event()
        closing = asyncio.Event()
        allowed = asyncio.Event()

        async def retrieve(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async def close_client():
            closing.set()
            await allowed.wait()

        env.retrieve.side_effect = retrieve
        env.client.aclose.side_effect = close_client
        task = asyncio.create_task(env.call())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel("original disconnect")
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel("cleanup disconnect")
        await asyncio.sleep(0)
        assert not task.done()
        allowed.set()
        with pytest.raises(asyncio.CancelledError, match="original disconnect"):
            await task
        assert_released(env)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 429, 503])
def test_http_error_status_body_and_retry_after_are_preserved(env, status):
    response = httpx.Response(status, json={"error": {"message": "unavailable"}},
                              headers={"retry-after": "7"}, request=httpx.Request("GET", "https://upstream"))
    env.retrieve.side_effect = httpx.HTTPStatusError("upstream error", request=response.request, response=response)

    async def scenario():
        result = await env.call()
        assert result.status_code == status
        assert result.body == response.content
        assert result.headers["retry-after"] == "7"
        assert_released(env)
        env.client.aclose.assert_awaited_once()

    asyncio.run(scenario())


def test_request_error_keeps_connection_error_semantics(env):
    env.retrieve.side_effect = httpx.ReadTimeout("retrieve timed out")

    async def scenario():
        with pytest.raises(ChannelException):
            await env.call()
        assert_released(env)
        assert env.engine.routing_state()

    asyncio.run(scenario())


def test_terminal_stream_uses_fresh_session_and_defers_accounting(env, monkeypatch):
    payload = {"id": "resp_1", "status": "completed", "usage": {"input_tokens": 3}}
    env.chunks = [b'data: {"type":"response.completed","response":' + json.dumps(payload).encode() + b'}\n\n']
    stream_db = SimpleNamespace(get=AsyncMock(return_value=env.channel), commit=AsyncMock())

    class Session:
        async def __aenter__(self):
            return stream_db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(endpoint, "async_session_maker", Session)
    monkeypatch.setattr(endpoint, "get_response_route", AsyncMock(return_value=env.route))
    monkeypatch.setattr(endpoint, "reload_principal", AsyncMock(return_value=env.token))

    async def scenario():
        response = await env.call()
        env.account.assert_not_awaited()
        async for _ in response.body_iterator:
            pass
        env.account.assert_awaited_once()
        assert env.account.call_args.args == (stream_db,)
        assert env.account.call_args.kwargs["payload"] == payload
        assert env.route.status == "completed"
        stream_db.commit.assert_awaited_once()
        env.db.commit.assert_not_awaited()
        assert_released(env)

    asyncio.run(scenario())
