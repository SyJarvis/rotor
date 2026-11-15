import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from rotor.api.v1 import chat, responses
from rotor.core.exceptions import ChannelException
from tests.test_openai_codex_gateway import prepare, invoke
from tests.test_openai_codex_adapter import SSEStream, sse, terminal


@pytest.mark.parametrize("entrypoint", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failures", [1, 2, 3])
def test_connect_retry_after_expired_cooldown_runs_at_endpoint(
    monkeypatch, entrypoint, stream, failures,
):
    env = prepare(monkeypatch)
    module = chat if entrypoint == "chat" else responses
    channel = module.get_available_channels.return_value[0]
    now = [100.0]
    env.engine._clock = lambda: now[0]
    env.engine.mark_unavailable("model", channel, 1)
    now[0] += 1
    requests = []
    clients = []
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    def upstream(request):
        requests.append(request)
        if len(requests) <= failures:
            raise httpx.ConnectTimeout("TLS handshake timeout", request=request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=SSEStream(sse([
                {"type": "response.output_text.delta", "delta": "hello"}, terminal(),
            ])),
        )

    def client(**kwargs):
        value = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        clients.append(value)
        return value

    monkeypatch.setattr(module, "AsyncClient", client)
    if failures == 3:
        with pytest.raises(ChannelException):
            asyncio.run(invoke(env, entrypoint, stream))
        assert env.engine.routing_state()[0][2]["remaining_seconds"] == 10
        env.accounting.record_success.assert_not_awaited()
    else:
        result = asyncio.run(invoke(env, entrypoint, stream))
        assert "hello" in json.dumps(result)
        assert env.engine.routing_state() == []
        env.accounting.record_success.assert_awaited_once()
    assert len(requests) == min(failures + 1, 3)
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 2.0][:failures]
    assert env.engine._in_flight == {}
    assert all(value.is_closed for value in clients)


@pytest.mark.parametrize("entrypoint", ["chat", "responses"])
def test_ambiguous_protocol_failure_does_not_get_connect_replays(monkeypatch, entrypoint):
    env = prepare(monkeypatch)
    module = chat if entrypoint == "chat" else responses
    requests = []
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    def upstream(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("server disconnected after POST", request=request)

    monkeypatch.setattr(
        module, "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    with pytest.raises(ChannelException):
        asyncio.run(invoke(env, entrypoint))
    assert len(requests) == 1
    sleep.assert_not_awaited()
    assert env.engine._in_flight == {}
    assert env.engine.routing_state()[0][2]["cooldown_seconds"] == 30
