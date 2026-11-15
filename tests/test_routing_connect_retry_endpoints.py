"""Exercise connect retries through real endpoint loops and routing admissions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest
from rotor.services.session_leases import SessionLeasePreference


@pytest.mark.parametrize("endpoint_name", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("expired_cooldown", [False, True])
def test_connect_timeout_retries_same_channel_after_release(
    monkeypatch, endpoint_name, stream, expired_cooldown,
):
    channel = SimpleNamespace(
        id=1, name="only-channel", type="openai", protocol="openai",
        enabled=True, priority=1, weight=1, extra={"max_in_flight": 1},
        model_mapping={}, models=["model"],
        base_url="https://upstream.invalid/v1", key="test-key",
    )
    now = [100.0]
    engine = RoutingEngine("fallback_order", clock=lambda: now[0])
    if expired_cooldown:
        engine.mark_unavailable("model", channel, 10)
        now[0] += 10
    db = AsyncMock()
    db.__aenter__.return_value = db
    token = SimpleNamespace(id=1, allowed_channels=None)
    accounting = SimpleNamespace(
        record_routing_decision=Mock(), record_success=AsyncMock(),
        record_failure=AsyncMock(), record_lease_success=AsyncMock(),
        extract_usage=AccountingService().extract_usage,
        streaming_usage=AccountingService().streaming_usage,
    )
    conversations = AsyncMock()
    conversations.start.return_value = SimpleNamespace(conversation_id="conv-retry")
    recorder = SimpleNamespace(record=AsyncMock())
    routing_settings = SimpleNamespace(
        affinity_enabled=False, session_lease_enabled=False,
        session_lease_reassess_seconds=300,
    )
    for module in (chat_endpoint, responses_endpoint):
        monkeypatch.setattr(module, "routing_engine", engine)
        monkeypatch.setattr(module, "get_available_channels", AsyncMock(return_value=[channel]))
        monkeypatch.setattr(module, "get_session_lease_preference", AsyncMock(return_value=SessionLeasePreference(None)))
        monkeypatch.setattr(module, "accounting_service", accounting)
        monkeypatch.setattr(module, "attempt_recorder", recorder)
        monkeypatch.setattr(module, "conversation_store", conversations)
        monkeypatch.setattr(module, "async_session_maker", lambda: db)
        monkeypatch.setattr(module, "application_settings", SimpleNamespace(get=lambda: SimpleNamespace(routing=routing_settings)))
    monkeypatch.setattr(chat_endpoint, "reload_principal", AsyncMock(return_value=token))
    monkeypatch.setattr(responses_endpoint, "save_response_route", AsyncMock())
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    seen = []
    clients = []

    def upstream(request):
        seen.append(request)
        # The second request needs a newly acquired slot, even after an expired
        # cooldown. A release-induced cooldown used to suppress this retry.
        assert engine.diagnose([channel], "model")[0]["in_flight"] == 1
        assert engine.routing_state() == []
        if len(seen) == 1:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        assert len(seen) == 2
        if stream:
            chunks = [
                {"id": "chat-retry", "object": "chat.completion.chunk", "model": "model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                {"id": "chat-retry", "object": "chat.completion.chunk", "model": "model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "chat-retry", "object": "chat.completion", "model": "model", "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def create_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)
        clients.append(client)
        return client

    module = chat_endpoint if endpoint_name == "chat" else responses_endpoint
    monkeypatch.setattr(module, "AsyncClient", create_client)

    async def scenario():
        if endpoint_name == "chat":
            request = ChatCompletionRequest(model="model", messages=[{"role": "user", "content": "hello"}], stream=stream)
            endpoint = module.chat_completions
            path = "/v1/chat/completions"
        else:
            request = ResponsesRequest(model="model", input="hello", stream=stream)
            endpoint = module.create_response
            path = "/v1/responses"
        http_request = Request({"type": "http", "method": "POST", "path": path, "headers": [], "client": ("127.0.0.1", 1)})
        headers = Response()
        result = await endpoint(request, http_request, headers, db, token)
        if stream:
            chunks = [chunk async for chunk in result.body_iterator]
            assert any("ok" in chunk for chunk in chunks)
            assert not any("stream_error" in chunk or "response.failed" in chunk for chunk in chunks)
            assert result.headers["X-Rotor-Channel"] == channel.name
        elif endpoint_name == "chat":
            assert result["choices"][0]["message"]["content"] == "ok"
        else:
            assert result["status"] == "completed"
            assert result["output"][0]["content"][0]["text"] == "ok"

    asyncio.run(scenario())
    assert len(seen) == 2
    assert seen[0].url == seen[1].url
    assert seen[0].content == seen[1].content
    sleep.assert_awaited_once_with(0.5)
    accounting.record_failure.assert_awaited_once()
    accounting.record_success.assert_awaited_once()
    failed = accounting.record_failure.call_args.kwargs["admission"]
    succeeded = accounting.record_success.call_args.kwargs["admission"]
    assert failed is not succeeded and failed.released and succeeded.released
    assert engine.diagnose([channel], "model")[0]["in_flight"] == 0
    assert engine.adaptive.score("model", channel).load_utility == 1
    assert engine.routing_state() == []
    assert all(client.is_closed for client in clients)
