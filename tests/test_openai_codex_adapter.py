import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.providers.openai_codex import OpenAICodexAdapter
from rotor.channels.presets import provider_defaults
from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError, classify_error_status, format_error_message
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import ChatCompletionRequest
from rotor.services import chatgpt_oauth


ACCESS = "mock-access-secret"
REFRESH = "mock-refresh-secret"
ACCOUNT = "mock-account-id"
USAGE = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
         "input_tokens_details": {"cached_tokens": 8},
         "output_tokens_details": {"reasoning_tokens": 2}}


def channel(**fields):
    return SimpleNamespace(
        **{
            "id": 1, "name": "codex", "type": "openai_codex", "protocol": "openai_responses",
            "auth_type": "oauth", "base_url": "https://should-not-be-called.invalid/custom",
            "key": "", "extra": {}, "model_mapping": {"model": "provider-model"},
            "oauth_refresh_token": REFRESH, "enabled": True, "priority": 1, "weight": 1,
            **fields,
        }
    )


def result(status="completed", **fields):
    return {
        "id": "resp-codex", "object": "response", "model": "provider-model", "status": status,
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": deepcopy(USAGE), **fields,
    }


def terminal(payload=None):
    payload = result() if payload is None else payload
    return {"type": f"response.{payload['status']}", "response": payload}


def sse(events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


class SSEStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content.encode() if isinstance(content, str) else content

    async def __aiter__(self):
        yield self.content


def request(mode="chat", stream=False, **fields):
    value = ChatCompletionRequest.model_validate({
        "model": "model", "messages": [{"role": "system", "content": "Follow the rules"},
                                        {"role": "user", "content": "hi"}],
        "stream": stream, **fields,
    })
    if mode == "native":
        value.responses_payload = {"model": "model", "input": "hi", "stream": stream, "instructions": "Native instructions"}
    elif mode == "anthropic":
        value.anthropic_payload = {"model": "model", "max_tokens": 16, "messages": []}
    return value


def exercise(monkeypatch, *, mode="chat", stream=False, events=None, body=None, status=200,
             config=None, value=None, observed=None, resolver_error=None, headers=None):
    resolver = AsyncMock(
        return_value=chatgpt_oauth.OAuthCredentials(access_token=ACCESS, account_id=ACCOUNT, expires_at=123),
        side_effect=resolver_error,
    )
    monkeypatch.setattr(chatgpt_oauth, "resolve_oauth_credentials", resolver)
    config = channel() if config is None else config
    value = request(mode, stream) if value is None else value
    captured = {}

    def upstream(http_request):
        captured["request"] = http_request
        if status != 200:
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=SSEStream(body if body is not None else sse(events if events is not None else [
                                  {"type": "response.output_text.delta", "delta": "hello"}, terminal(),
                              ])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter = AdapterFactory.create_adapter(config, client)
            captured["adapter"] = adapter
            response = await adapter.make_request(value)
            captured["response"] = response
            captured["transport_streamed"] = not response.is_stream_consumed
            try:
                if not value.stream:
                    converted = await adapter.convert_response(response, value)
                else:
                    converted = observed if observed is not None else []
                    async for event in adapter.stream_convert_response(response, value):
                        converted.append(event)
                captured["converted"] = converted
            finally:
                await response.aclose()
            return captured

    captured["resolver"] = resolver
    captured["value"] = value
    return asyncio.run(run())


def test_codex_factory_and_preset_are_specific():
    adapter = AdapterFactory.create_adapter(channel(), None)
    assert isinstance(adapter, OpenAICodexAdapter)
    preset = provider_defaults("openai_codex", "openai_responses")
    assert preset["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert preset["request_path"] == "/responses"
    assert preset["models_path"] == "/models"
    assert preset["auth_type"] == "bearer"


@pytest.mark.parametrize("mode", ["native", "chat", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_sse_transport_preserves_each_client_protocol_and_usage(monkeypatch, mode, stream):
    captured = exercise(monkeypatch, mode=mode, stream=stream)
    outgoing = captured["request"]
    assert str(outgoing.url) == "https://chatgpt.com/backend-api/codex/responses"
    body = json.loads(outgoing.content)
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "provider-model"
    assert body["instructions"] == ("Native instructions" if mode == "native" else "Follow the rules")
    assert captured["value"].stream == stream
    assert captured["transport_streamed"]
    assert captured["response"].is_closed
    captured["resolver"].assert_awaited_once()
    converted = captured["converted"][-1] if stream else captured["converted"]
    if mode == "native":
        payload = converted["response"] if stream else converted
        assert payload == result()
        usage = AccountingService().extract_usage(payload)
    else:
        assert converted["choices"][0]["finish_reason"] == "stop"
        if not stream:
            assert converted["choices"][0]["message"]["content"] == "hello"
        usage = AccountingService().extract_usage(converted)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, usage.reasoning_tokens) == (12, 3, 8, 2)
    assert usage.usage_source == "provider"


def test_headers_cannot_override_oauth_authentication_or_account(monkeypatch):
    config = channel(extra={
        "headers": {"authorization": "Bearer attack", "CHATGPT-ACCOUNT-ID": "attack",
                    "Cookie": "attack", "X-Api-Key": "attack", "Originator": "attack",
                    "ACCEPT": "application/json", "x-custom": "static"},
        "forward_headers": ["authorization", "chatgpt-account-id", "originator", "x-custom", "x-session"],
        "request_path": "https://attack.invalid/responses",
    })
    value = request()
    value.inbound_headers = {"authorization": "Bearer incoming", "chatgpt-account-id": "incoming",
                             "originator": "incoming", "x-custom": "incoming", "x-session": "session"}
    outgoing = exercise(monkeypatch, config=config, value=value)["request"]
    assert outgoing.headers["authorization"] == f"Bearer {ACCESS}"
    assert outgoing.headers["chatgpt-account-id"] == ACCOUNT
    assert outgoing.headers["originator"] == "rotor"
    assert outgoing.headers["accept"] == "text/event-stream"
    assert outgoing.headers["openai-beta"] == "responses=experimental"
    assert outgoing.headers["x-custom"] == "static"
    assert outgoing.headers["x-session"] == "session"
    assert "cookie" not in outgoing.headers and "x-api-key" not in outgoing.headers
    assert config.key == "" and "refresh" not in outgoing.content.decode()


def test_native_body_preserves_tools_and_explicit_generation_parameters(monkeypatch):
    value = request("native")
    value.responses_payload.update({
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        "max_output_tokens": 123, "top_p": 0.8, "temperature": 0.1, "user": "caller",
        "instructions": [{"type": "input_text", "text": "Be precise"}],
        "store": False,
    })
    original = deepcopy(value.responses_payload)
    body = json.loads(exercise(monkeypatch, value=value)["request"].content)
    assert body["instructions"] == "Be precise"
    for key in ("tools", "max_output_tokens", "top_p", "temperature", "user"):
        assert body[key] == original[key]
    assert value.responses_payload == original


@pytest.mark.parametrize("input_text", ["hi", ""])
def test_codex_native_string_input_becomes_a_user_message(monkeypatch, input_text):
    value = request("native")
    value.responses_payload["input"] = input_text
    body = json.loads(exercise(monkeypatch, value=value)["request"].content)
    assert body["input"] == [{
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": input_text}],
    }]
    assert value.responses_payload["input"] == input_text


def test_codex_native_input_list_preserves_provider_items(monkeypatch):
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "reasoning", "id": "reasoning-1", "encrypted_content": "opaque", "summary": []},
        {"type": "function_call_output", "call_id": "call-1", "output": "done"},
    ]
    value = request("native")
    value.responses_payload["input"] = deepcopy(items)
    body = json.loads(exercise(monkeypatch, value=value)["request"].content)
    assert body["input"] == items
    assert value.responses_payload["input"] == items


@pytest.mark.parametrize("stream", [False, True])
def test_ordinary_responses_preserves_native_string_input(stream):
    value = request("native", stream=stream)
    adapter = AdapterFactory.create_adapter(channel(type="openai", auth_type="api_key"), None)
    body = asyncio.run(adapter.convert_request(value))
    assert body["input"] == "hi"
    assert value.responses_payload["input"] == "hi"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_system", [False, True])
def test_codex_omitted_sampling_survives_system_validator_and_stream_copy(monkeypatch, stream, with_system):
    messages = [{"role": "user", "content": "hi"}]
    if with_system:
        messages.append({"role": "system", "content": "Follow the rules"})
    value = request(stream=stream, messages=messages)
    original_fields = set(value.model_fields_set)
    assert "temperature" not in original_fields and "top_p" not in original_fields
    if with_system:
        assert value.messages[0].role.value == "system"
    body = json.loads(exercise(monkeypatch, value=value)["request"].content)
    assert "temperature" not in body and "top_p" not in body
    assert body["stream"] is True
    assert value.model_fields_set == original_fields


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["chat", "native"])
@pytest.mark.parametrize("fields", [{"temperature": 1.0}, {"top_p": 1.0}, {"temperature": 0.3, "top_p": 0.8}])
def test_codex_explicit_sampling_is_preserved(monkeypatch, stream, mode, fields):
    if mode == "native":
        value = request(mode, stream)
        value.responses_payload.update(fields)
    else:
        value = request(mode, stream, **fields)
    body = json.loads(exercise(monkeypatch, value=value)["request"].content)
    for name in ("temperature", "top_p"):
        if name in fields:
            assert body[name] == fields[name]
        else:
            assert name not in body


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["openai_responses", "openai"])
def test_ordinary_adapters_preserve_chat_sampling_defaults(stream, protocol):
    value = request(stream=stream)
    adapter = AdapterFactory.create_adapter(channel(type="openai", auth_type="api_key", protocol=protocol), None)
    body = asyncio.run(adapter.convert_request(value))
    assert body["temperature"] == 1.0 and body["top_p"] == 1.0


def test_missing_instructions_has_nonempty_default(monkeypatch):
    value = request("native")
    value.responses_payload["instructions"] = "  "
    assert json.loads(exercise(monkeypatch, value=value)["request"].content)["instructions"] == "You are a helpful assistant."


@pytest.mark.parametrize("fields", [{"store": True}, {"background": True},
                                     {"previous_response_id": "resp-old"}, {"conversation": "conv-old"}])
def test_persistent_requests_fail_with_explicit_400(monkeypatch, fields):
    value = request("native")
    value.responses_payload.update(fields)
    with pytest.raises(UpstreamProtocolError) as captured:
        exercise(monkeypatch, value=value)
    assert classify_error_status(captured.value) == 400


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tail", ["event: response.output_text.delta\ndata: {malformed\n\n",
                                    sse([{"type": "error", "message": "late error"}])])
def test_late_failure_retains_usage_and_withholds_success(monkeypatch, stream, tail):
    observed = []
    with pytest.raises(UpstreamProtocolError) as captured:
        exercise(monkeypatch, mode="native", stream=stream, body=sse([terminal()]) + tail, observed=observed)
    assert captured.value.provider_usage == USAGE
    assert captured.value.upstream_status == 200
    assert observed == []


@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_result_keeps_reason_and_usage(monkeypatch, stream):
    payload = result("incomplete", incomplete_details={"reason": "max_output_tokens"})
    captured = exercise(monkeypatch, stream=stream, events=[terminal(payload)])
    converted = captured["converted"][-1] if stream else captured["converted"]
    assert converted["choices"][0]["finish_reason"] == "length"
    assert AccountingService().extract_usage(converted).cached_tokens == 8


@pytest.mark.parametrize("stream", [False, True])
def test_stream_error_redacts_tokens_and_keeps_overload_classification(monkeypatch, stream):
    message = f"overloaded {ACCESS} {REFRESH} {ACCOUNT}"
    with pytest.raises(UpstreamOverloaded) as captured:
        exercise(monkeypatch, stream=stream, events=[{"type": "error", "message": message, "code": "overloaded_error"}])
    assert all(secret not in format_error_message(captured.value) for secret in (ACCESS, REFRESH, ACCOUNT))
    assert captured.value.upstream_status == 200


def test_http_error_diagnostics_redact_oauth_tokens_and_sensitive_fields(monkeypatch):
    with pytest.raises(httpx.HTTPStatusError) as captured:
        exercise(monkeypatch, status=401, headers={"x-request-id": ACCESS}, body={
            "error": {"message": f"rejected {ACCESS} {REFRESH} {ACCOUNT}"},
            "access_token": "another-access", "refresh_token": "another-refresh",
        })
    message = format_error_message(captured.value)
    assert all(secret not in message for secret in (ACCESS, REFRESH, ACCOUNT, "another-access", "another-refresh"))
    assert classify_error_status(captured.value) == 401


def test_structured_stream_error_redacts_sensitive_fields(monkeypatch):
    with pytest.raises(UpstreamProtocolError) as captured:
        exercise(monkeypatch, events=[{"type": "error", "message": {"access_token": "other-token"}}])
    assert "other-token" not in format_error_message(captured.value)


@pytest.mark.parametrize("stream", [False, True])
def test_anthropic_unrepresentable_incomplete_keeps_known_usage(monkeypatch, stream):
    payload = result("incomplete", incomplete_details={"reason": "max_output_tokens"})
    with pytest.raises(UpstreamProtocolError) as captured:
        exercise(monkeypatch, mode="anthropic", stream=stream, events=[terminal(payload)])
    assert captured.value.provider_usage == USAGE
    assert captured.value.upstream_status == 200


def test_oauth_resolution_failure_stays_an_identifiable_safe_401(monkeypatch):
    with pytest.raises(chatgpt_oauth.OAuthError) as captured:
        exercise(monkeypatch, resolver_error=chatgpt_oauth.OAuthError("ChatGPT login is required", status_code=401))
    assert classify_error_status(captured.value) == 401


@pytest.mark.parametrize("capability", ["embeddings", "image_generation", "responses_resources", "responses_persistence"])
def test_unsupported_routes_are_rejected_even_with_explicit_capabilities(capability):
    config = channel(extra={"capabilities": [capability]})
    assert RoutingEngine().route([config], "model", required_capabilities={capability}).candidates == []
    ordinary = channel(type="openai", key="mock-key")
    assert RoutingEngine().route([ordinary], "model", required_capabilities={capability}).candidates == [ordinary]


def test_codex_still_supports_native_tools_streaming_vision_and_reasoning():
    config = channel()
    assert RoutingEngine().route([config], "model", required_capabilities={
        "responses_native", "stream", "function_call", "vision", "reasoning_effort",
    }).candidates == [config]
