import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.anthropic as anthropic_endpoint
import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.images as images_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.core.exceptions import ChannelsTemporarilyUnavailable
from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest
from tests.test_attempt_latency import CompletionStream, environment


ENDPOINTS = (chat_endpoint, anthropic_endpoint, responses_endpoint, images_endpoint)
PROTOCOLS = ("chat", "anthropic", "responses", "images")


async def invoke(env, protocol, *, stream=False, **fields):
    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "client": ("127.0.0.1", 1),
    })
    common = dict(model="model", stream=stream, **fields)
    if protocol == "chat":
        body = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], **common)
        handler = chat_endpoint.chat_completions
    elif protocol == "anthropic":
        body = AnthropicMessageRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=100, **common)
        handler = anthropic_endpoint.messages
    elif protocol == "responses":
        body = ResponsesRequest(input="hi", **common)
        handler = responses_endpoint.create_response
    else:
        body = images_endpoint.ImageGenerationRequest(prompt="hi", size="1024x1024", **common)
        handler = images_endpoint.generate_image
    return await handler(body, request, Response(), env.db, env.token)


def prepare(env, monkeypatch, upstream, *, cooling=0):
    env.channels[:] = env.channels[:1]
    env.engine._clock = env.clock.monotonic
    env.engine.mark_unavailable("model", env.channels[0], cooling)
    clients = []

    def client_factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)
        clients.append(client)
        return client

    for endpoint in ENDPOINTS:
        monkeypatch.setattr(endpoint, "AsyncClient", client_factory)
    return clients


def assert_released(env, *, recovered=False, delay=30):
    assert env.engine.adaptive._stats[("model", 1)].inflight == 0
    decision = env.engine.route(env.channels, "model")
    if recovered:
        assert decision.candidates == env.channels
        assert ("model", 1) not in env.engine._cooldowns
    else:
        assert decision.candidates == []
        assert decision.retry_after_seconds == delay
        assert env.engine._cooldowns[("model", 1)].probe is None


def completed_response():
    return httpx.Response(200, json={
        "id": "chat_1", "model": "model",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cooling_and_stale_candidates_never_call_provider(environment, monkeypatch, protocol):
    env = environment
    upstream = AsyncMock(side_effect=AssertionError("must not call provider"))
    clients = prepare(env, monkeypatch, upstream, cooling=12.1)

    async def exercise():
        with pytest.raises(ChannelsTemporarilyUnavailable) as caught:
            await invoke(env, protocol)
        assert caught.value.headers == {"Retry-After": "13"}
        assert not clients
        env.clock.advance(13)
        original = env.engine.admit_attempt

        def reject_stale(model, channel):
            env.engine.mark_unavailable(model, channel, 120)
            return original(model, channel)

        monkeypatch.setattr(env.engine, "admit_attempt", reject_stale)
        with pytest.raises(ChannelsTemporarilyUnavailable) as caught:
            await invoke(env, protocol)
        assert caught.value.headers == {"Retry-After": "120"}
        if protocol != "images":
            assert chat_endpoint.conversation_store.finish.await_args.args[1] == "failed"

    asyncio.run(exercise())
    upstream.assert_not_awaited()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cancellation_during_provider_call_releases_single_probe(environment, monkeypatch, protocol):
    env = environment
    entered = asyncio.Event()

    async def upstream(request):
        entered.set()
        await asyncio.Event().wait()

    clients = prepare(env, monkeypatch, upstream)

    async def exercise():
        task = asyncio.create_task(invoke(env, protocol))
        await entered.wait()
        with pytest.raises(ChannelsTemporarilyUnavailable) as caught:
            await invoke(env, protocol)
        assert caught.value.headers == {"Retry-After": "1"}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert_released(env)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("provider_success", [False, True])
def test_accounting_failure_cannot_leak_or_misclassify_probe(environment, monkeypatch, protocol, provider_success):
    env = environment

    async def upstream(request):
        if provider_success:
            return completed_response()
        return httpx.Response(503, headers={"Retry-After": "120"}, json={"error": {"message": "busy"}})

    clients = prepare(env, monkeypatch, upstream)
    monkeypatch.setattr(env.accounting, "record_success", AsyncMock(side_effect=RuntimeError("database unavailable")))
    monkeypatch.setattr(env.accounting, "record_failure", AsyncMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(invoke(env, protocol))
    assert_released(env, recovered=provider_success, delay=120)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_adapter_or_request_creation_failure_releases_probe(environment, monkeypatch, protocol):
    env = environment
    clients = prepare(env, monkeypatch, AsyncMock(side_effect=AssertionError("must not call provider")))
    endpoint = ENDPOINTS[PROTOCOLS.index(protocol)]
    if protocol == "images":
        monkeypatch.setattr(endpoint, "image_generation_headers", Mock(side_effect=RuntimeError("adapter unavailable")))
    else:
        monkeypatch.setattr(endpoint, "AdapterFactory", SimpleNamespace(
            create_adapter=Mock(side_effect=RuntimeError("adapter unavailable")),
        ))
    with pytest.raises(RuntimeError, match="adapter unavailable"):
        asyncio.run(invoke(env, protocol))
    assert_released(env)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("started", [False, True])
def test_stream_handoff_releases_on_send_failure_or_completion(environment, monkeypatch, protocol, started):
    env = environment
    responses = []

    async def upstream(request):
        response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                 stream=CompletionStream(env.clock, False, protocol == "images"))
        responses.append(response)
        return response

    clients = prepare(env, monkeypatch, upstream)

    async def exercise():
        response = await invoke(env, protocol, stream=True)
        assert env.engine.adaptive._stats[("model", 1)].inflight == 1
        assert env.engine.route(env.channels, "model").candidates == []
        if started:
            assert [chunk async for chunk in response.body_iterator]
        else:
            async def fail_send(message):
                raise RuntimeError("send start failed")

            with pytest.raises(RuntimeError, match="send start failed"):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), fail_send)

    asyncio.run(exercise())
    assert_released(env, recovered=started)
    assert all(client.is_closed for client in clients)
    assert all(response.is_closed for response in responses)


@pytest.mark.parametrize("operation", ["input_tokens", "compact"])
@pytest.mark.parametrize("outcome", ["success", "retry", "cancel"])
def test_native_collection_uses_same_recovery_admission(environment, monkeypatch, operation, outcome):
    env = environment

    async def upstream(request):
        assert request.url.path.endswith("/" + operation)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "retry":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"input_tokens": 3})

    clients = prepare(env, monkeypatch, upstream)
    env.channels[0].protocol = "openai_responses"

    async def exercise():
        call = responses_endpoint._native_collection_request(
            body={"model": "model", "input": "hi"}, operation=operation,
            token=env.token, db=env.db,
        )
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            response = await call
            assert response.status_code == (200 if outcome == "success" else 429)

    asyncio.run(exercise())
    assert_released(env, recovered=outcome == "success", delay=120 if outcome == "retry" else 30)
    assert all(client.is_closed for client in clients)


def test_background_accepted_releases_probe_without_waiting_for_poll(environment, monkeypatch):
    env = environment
    payload = {"id": "resp_bg", "model": "model", "object": "response", "status": "queued", "output": [], "usage": None}
    clients = prepare(env, monkeypatch, AsyncMock(return_value=httpx.Response(200, json=payload)))
    env.channels[0].protocol = "openai_responses"
    monkeypatch.setattr(responses_endpoint, "save_response_route", AsyncMock())
    result = asyncio.run(invoke(env, "responses", background=True))
    assert result["status"] == "queued"
    assert_released(env, recovered=True)
    assert env.engine.adaptive._stats[("model", 1)].observations == 1
    assert all(client.is_closed for client in clients)


def test_previous_response_binding_cannot_bypass_cooling_or_switch_channel(environment, monkeypatch):
    env = environment
    upstream = AsyncMock(side_effect=AssertionError("must not call provider"))
    prepare(env, monkeypatch, upstream, cooling=120)
    env.channels[0].protocol = "openai_responses"
    monkeypatch.setattr(responses_endpoint, "get_response_route", AsyncMock(return_value=SimpleNamespace(channel_id=1, conversation_id="conv")))
    monkeypatch.setattr(env.db, "get", AsyncMock(return_value=env.channels[0]))
    with pytest.raises(ChannelsTemporarilyUnavailable) as caught:
        asyncio.run(invoke(env, "responses", previous_response_id="resp_bound"))
    assert caught.value.headers == {"Retry-After": "120"}
    upstream.assert_not_awaited()
    responses_endpoint.get_session_lease_preference.assert_not_awaited()
