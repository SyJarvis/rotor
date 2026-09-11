import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.anthropic as anthropic_endpoint
import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.application_settings import ApplicationSettings, RoutingSettings
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest
from rotor.services.session_leases import SessionLeasePreference


def _setup(monkeypatch, protocol, *, due=True, fail_native=False, interval=300,
           affinity=True, cooling=False, missing_vision=False, state_bound=False):
    endpoints = {
        "openai_chat": (chat_endpoint, chat_endpoint.chat_completions, "openai", "anthropic"),
        "anthropic_messages": (anthropic_endpoint, anthropic_endpoint.messages, "anthropic", "openai"),
        "openai_responses": (responses_endpoint, responses_endpoint.create_response, "openai_responses", "openai"),
    }
    endpoint, handler, native_protocol, fallback_protocol = endpoints[protocol]
    channels = [
        SimpleNamespace(
            id=index, name=f"channel-{index}", protocol=wire, type="openai",
            enabled=True, priority=10, weight=1, extra={}, model_mapping={},
        )
        for index, wire in [(1, native_protocol), (2, fallback_protocol)]
    ]
    if state_bound:
        channels[1].protocol = "openai_responses"
    if missing_vision:
        channels[0].extra = {"capabilities": []}
    settings = ApplicationSettings(routing=RoutingSettings(
        strategy="fallback_order", session_lease_reassess_seconds=interval,
        protocol_affinity_enabled=affinity,
    ))
    engine = RoutingEngine(strategy="fallback_order")
    engine.configure_adaptive(settings.routing)
    if cooling:
        engine.mark_unavailable("model-a", channels[0], 3600)

    async def preference(_db, **kwargs):
        return SessionLeasePreference(2, due and kwargs["reassess_seconds"] > 0)

    read_preference = AsyncMock(side_effect=preference)
    monkeypatch.setattr(endpoint, "get_session_lease_preference", read_preference)
    monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=channels))
    monkeypatch.setattr(endpoint, "routing_engine", engine)
    monkeypatch.setattr(endpoint, "application_settings", SimpleNamespace(get=lambda: settings))
    monkeypatch.setattr(endpoint, "AsyncClient", Mock(return_value=SimpleNamespace(aclose=AsyncMock())))
    monkeypatch.setattr(endpoint, "AdapterFactory", SimpleNamespace(create_adapter=Mock(return_value=object())))
    monkeypatch.setattr(endpoint, "accounting_service", SimpleNamespace(
        record_routing_decision=Mock(), record_failure=AsyncMock(),
    ))
    monkeypatch.setattr(endpoint, "attempt_recorder", SimpleNamespace(record=AsyncMock()))
    monkeypatch.setattr(endpoint, "conversation_store", SimpleNamespace(
        start=AsyncMock(return_value=SimpleNamespace(conversation_id="session-a")),
        append_routing=AsyncMock(), append_error=AsyncMock(), finish=AsyncMock(),
    ))
    seen = []

    async def complete(*args, **kwargs):
        channel = args[3 if protocol == "anthropic_messages" else 2]
        seen.append((channel.id, kwargs["lease_migration_reason"]))
        if channel.id == 1 and fail_native:
            request = httpx.Request("POST", "https://upstream.example/api")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        if protocol == "openai_responses":
            return {"object": "response", "status": "completed", "output": []}
        return {"ok": True}

    monkeypatch.setattr(endpoint, "_handle_non_streaming_request", complete)
    fields = {"model": "model-a"}
    if protocol == "openai_responses":
        fields["input"] = "hello"
        if missing_vision:
            fields["input"] = [{"role": "user", "content": [{
                "type": "input_image", "image_url": "https://image.example/a.png",
            }]}]
        if state_bound:
            fields["previous_response_id"] = "resp-bound"
        request = ResponsesRequest.model_validate(fields)
    else:
        content = "hello"
        if missing_vision:
            content = [{"type": "image_url", "image_url": {"url": "https://image.example/a.png"}}]
            if protocol == "anthropic_messages":
                content = [{"type": "image", "source": {"type": "url", "url": "https://image.example/a.png"}}]
        fields["messages"] = [{"role": "user", "content": content}]
        request = (
            AnthropicMessageRequest.model_validate({**fields, "max_tokens": 128})
            if protocol == "anthropic_messages" else ChatCompletionRequest.model_validate(fields)
        )
    db = SimpleNamespace(commit=AsyncMock(), get=AsyncMock(return_value=channels[1]))
    if state_bound:
        monkeypatch.setattr(endpoint, "get_response_route", AsyncMock(return_value=SimpleNamespace(
            channel_id=2, conversation_id="session-a",
        )))
        engine.route = Mock(side_effect=AssertionError("state binding must bypass routing"))
    http_request = Request({
        "type": "http", "method": "POST", "path": "/v1/request",
        "headers": [(b"x-rotor-session-id", b"session-a")],
        "client": ("127.0.0.1", 1),
    })
    call = handler(request, http_request, Response(), db=db, token=SimpleNamespace(id=7, allowed_channels=None))
    return call, seen, read_preference


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
@pytest.mark.parametrize("fail_native", [False, True])
def test_due_lease_reassessment_reaches_native_or_defers_after_failed_recovery(monkeypatch, protocol, fail_native):
    call, seen, preference = _setup(monkeypatch, protocol, fail_native=fail_native)
    asyncio.run(call)
    assert seen == (
        [(1, "protocol_recovered"), (2, "protocol_reassessment_deferred")]
        if fail_native else [(1, "protocol_recovered")]
    )
    kwargs = preference.await_args.kwargs
    assert kwargs["request_protocol"] == protocol
    assert kwargs["reassess_seconds"] == 300
    assert kwargs["active_native_channel_ids"] == {1}


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
@pytest.mark.parametrize("options", [
    {"due": False}, {"interval": 0}, {"affinity": False},
    {"cooling": True}, {"missing_vision": True},
])
def test_lease_reassessment_keeps_existing_lease_when_disabled_or_no_native_is_eligible(monkeypatch, protocol, options):
    call, seen, preference = _setup(monkeypatch, protocol, **options)
    asyncio.run(call)
    assert seen == [(2, "request_success")]
    if options.get("cooling") or options.get("missing_vision"):
        assert preference.await_args.kwargs["active_native_channel_ids"] == set()


def test_stateful_responses_bypass_lease_reassessment(monkeypatch):
    call, seen, preference = _setup(monkeypatch, "openai_responses", state_bound=True)
    asyncio.run(call)
    assert seen == [(2, "state_binding")]
    preference.assert_not_awaited()
