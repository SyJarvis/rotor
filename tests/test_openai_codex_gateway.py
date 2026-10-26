import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.anthropic as anthropic_endpoint
import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest
from rotor.services import chatgpt_oauth
from rotor.services.session_leases import SessionLeasePreference

from tests.test_openai_codex_adapter import ACCESS, ACCOUNT, SSEStream, channel, result, sse, terminal


def prepare(monkeypatch, *, fallback=False):
    codex = channel(priority=100)
    ordinary = channel(id=2, name="api-key", type="openai", auth_type="api_key", protocol="openai",
                       key="mock-api-key", base_url="https://api-key.invalid/v1", priority=1)
    channels = [codex, ordinary] if fallback else [codex]
    engine = RoutingEngine("fallback_order")
    engine.protocol_affinity_enabled = False
    token = SimpleNamespace(id=1, allowed_channels=None)
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.get.return_value = token
    real_accounting = AccountingService()
    accounting = SimpleNamespace(
        record_routing_decision=Mock(), record_success=AsyncMock(), record_failure=AsyncMock(),
        record_lease_success=AsyncMock(), extract_usage=real_accounting.extract_usage,
        streaming_usage=real_accounting.streaming_usage,
    )
    conversation = AsyncMock()
    conversation.start.return_value = SimpleNamespace(conversation_id="conv-test")
    for module in (chat_endpoint, responses_endpoint, anthropic_endpoint):
        monkeypatch.setattr(module, "get_available_channels", AsyncMock(return_value=channels))
        monkeypatch.setattr(module, "get_session_lease_preference", AsyncMock(return_value=SessionLeasePreference(None)))
        monkeypatch.setattr(module, "routing_engine", engine)
        monkeypatch.setattr(module, "accounting_service", accounting)
        monkeypatch.setattr(module, "attempt_recorder", SimpleNamespace(record=AsyncMock()))
        monkeypatch.setattr(module, "conversation_store", conversation)
        monkeypatch.setattr(module, "async_session_maker", lambda: db)
    monkeypatch.setattr(responses_endpoint, "save_response_route", AsyncMock())
    resolver = AsyncMock(
        return_value=chatgpt_oauth.OAuthCredentials(ACCESS, ACCOUNT, 123),
        side_effect=chatgpt_oauth.OAuthError("ChatGPT login is required") if fallback else None,
    )
    monkeypatch.setattr(chatgpt_oauth, "resolve_oauth_credentials", resolver)
    sent = []

    def upstream(request):
        sent.append(request)
        if request.url.host == "chatgpt.com":
            body = json.loads(request.content)
            if not isinstance(body.get("input"), list):
                return httpx.Response(400, json={"detail": "Input must be a list"})
            for parameter in ("temperature", "top_p"):
                if parameter in body:
                    return httpx.Response(400, json={"detail": f"Unsupported parameter: {parameter}"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SSEStream(sse([
                {"type": "response.output_text.delta", "delta": "hello"}, terminal(),
            ])))
        assert request.url.host == "api-key.invalid"
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 123, "model": "provider-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "api fallback"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                      "prompt_tokens_details": {"cached_tokens": 8}},
        })

    for module in (chat_endpoint, responses_endpoint, anthropic_endpoint):
        monkeypatch.setattr(module, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return SimpleNamespace(token=token, db=db, accounting=accounting, sent=sent, resolver=resolver, engine=engine)


async def invoke(env, entrypoint, stream=False, **fields):
    value = {"model": "model", "stream": stream, "messages": [{"role": "user", "content": "hi"}], **fields}
    if entrypoint == "responses":
        request = ResponsesRequest(model="model", input="hi", stream=stream, **fields)
        handler = responses_endpoint.create_response
        path = "/v1/responses"
    elif entrypoint == "anthropic":
        request = AnthropicMessageRequest.model_validate({**value, "max_tokens": 123})
        handler = anthropic_endpoint.messages
        path = "/anthropic/v1/messages"
    else:
        request = ChatCompletionRequest.model_validate(value)
        handler = chat_endpoint.chat_completions
        path = "/v1/chat/completions"
    http_request = Request({"type": "http", "method": "POST", "path": path,
                            "headers": [], "client": ("127.0.0.1", 1)})
    response = await handler(request, http_request, Response(), db=env.db, token=env.token)
    if stream:
        parts = []
        async for part in response.body_iterator:
            parts.append(part.decode() if isinstance(part, bytes) else part)
        return "".join(parts)
    return response


@pytest.mark.parametrize("entrypoint", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_codex_success_reaches_gateway_accounting_for_all_client_protocols(monkeypatch, entrypoint, stream):
    env = prepare(monkeypatch)
    response = asyncio.run(invoke(env, entrypoint, stream))
    assert len(env.sent) == 1
    assert env.sent[0].headers["authorization"] == f"Bearer {ACCESS}"
    assert env.sent[0].headers["chatgpt-account-id"] == ACCOUNT
    assert json.loads(env.sent[0].content)["stream"] is True
    env.accounting.record_success.assert_awaited_once()
    env.accounting.record_failure.assert_not_awaited()
    success = env.accounting.record_success.await_args.kwargs
    assert success["channel"].type == "openai_codex"
    usage = success["usage"]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12, 3, 8)
    assert usage.usage_source == "provider"
    if stream:
        assert "hello" in response
        assert "response.failed" not in response and "stream_error" not in response
        assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[entrypoint] in response
    elif entrypoint == "responses":
        assert response == result()
    elif entrypoint == "anthropic":
        assert response["content"][0]["text"] == "hello"
        assert response["stop_reason"] == "end_turn"
    else:
        assert response["choices"][0]["message"]["content"] == "hello"


@pytest.mark.parametrize("stream", [False, True])
def test_native_string_input_succeeds_with_list_only_codex_and_accounting(monkeypatch, stream):
    env = prepare(monkeypatch)
    response = asyncio.run(invoke(env, "responses", stream))
    body = json.loads(env.sent[0].content)
    assert body["input"] == [{
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}],
    }]
    env.accounting.record_failure.assert_not_awaited()
    env.accounting.record_success.assert_awaited_once()
    usage = env.accounting.record_success.await_args.kwargs["usage"]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12, 3, 8)
    assert usage.usage_source == "provider"
    assert "response.completed" in response if stream else response == result()


@pytest.mark.parametrize("stream", [False, True])
def test_chat_omitted_sampling_succeeds_with_strict_codex_and_accounting(monkeypatch, stream):
    env = prepare(monkeypatch)
    response = asyncio.run(invoke(env, "chat", stream))
    body = json.loads(env.sent[0].content)
    assert "temperature" not in body and "top_p" not in body
    env.accounting.record_failure.assert_not_awaited()
    env.accounting.record_success.assert_awaited_once()
    usage = env.accounting.record_success.await_args.kwargs["usage"]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12, 3, 8)
    assert usage.usage_source == "provider"
    assert "[DONE]" in response if stream else response["choices"][0]["message"]["content"] == "hello"


@pytest.mark.parametrize("entrypoint", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("temperature", [0.0, 0.3, 1.0])
def test_explicit_temperature_succeeds_with_strict_codex_and_accounting(monkeypatch, entrypoint, stream, temperature):
    env = prepare(monkeypatch)
    response = asyncio.run(invoke(env, entrypoint, stream, temperature=temperature))
    assert len(env.sent) == 1
    assert "temperature" not in json.loads(env.sent[0].content)
    env.accounting.record_failure.assert_not_awaited()
    env.accounting.record_success.assert_awaited_once()
    usage = env.accounting.record_success.await_args.kwargs["usage"]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12, 3, 8)
    assert usage.usage_source == "provider"
    if stream:
        assert "hello" in response
        assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[entrypoint] in response
        assert "response.failed" not in response and "stream_error" not in response
    else:
        assert "hello" in json.dumps(response)


@pytest.mark.parametrize("entrypoint", ["chat", "responses", "anthropic"])
def test_oauth_401_uses_existing_api_key_fallback_and_records_both_attempts(monkeypatch, entrypoint):
    env = prepare(monkeypatch, fallback=True)
    response = asyncio.run(invoke(env, entrypoint))
    env.resolver.assert_awaited_once()
    assert len(env.sent) == 1 and env.sent[0].url.host == "api-key.invalid"
    assert env.sent[0].headers["authorization"] == "Bearer mock-api-key"
    env.accounting.record_failure.assert_awaited_once()
    env.accounting.record_success.assert_awaited_once()
    failure = env.accounting.record_failure.await_args.kwargs
    assert failure["channel"].type == "openai_codex"
    assert failure["provider_response"]["status_code"] == 401
    assert ACCESS not in failure["error_message"]
    success = env.accounting.record_success.await_args.kwargs
    assert success["channel"].type == "openai"
    assert success["usage"].cached_tokens == 8
    assert "api fallback" in json.dumps(response)
