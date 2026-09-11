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
from rotor.core.exceptions import ChannelException
from rotor.gateway.accounting import AccountingService
from rotor.gateway.capabilities import (
    anthropic_required_capabilities,
    chat_required_capabilities,
)
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest


def _channel(channel_id, protocol, capabilities=None, provider_type="openai"):
    return SimpleNamespace(
        id=channel_id,
        name=f"channel-{channel_id}",
        type=provider_type,
        protocol=protocol,
        priority=10,
        weight=1,
        enabled=True,
        extra={} if capabilities is None else {"capabilities": capabilities},
        model_mapping={},
        base_url="https://provider.example/v1",
        key="test-key",
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"tools": [{"type": "function", "function": {"name": "lookup"}}]}, {"function_call"}),
        ({"messages": [{"role": "assistant", "tool_calls": [{
            "id": "call-1", "function": {"name": "lookup", "arguments": "{}"},
        }]}]}, {"function_call"}),
        ({"messages": [{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]}, {"function_call"}),
        ({"stream": True, "messages": [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": "https://image.example/a.png"},
        }]}]}, {"stream", "vision"}),
        ({"response_format": {"type": "json_object"}}, {"openai_chat_native"}),
        ({"parallel_tool_calls": False}, {"openai_chat_native"}),
        ({"max_completion_tokens": 32}, {"openai_chat_native"}),
        ({"messages": [{"role": "assistant", "content": "ok", "reasoning_content": "thought"}]}, {"openai_chat_native"}),
        ({"reasoning_effort": "high"}, {"reasoning_effort"}),
    ],
)
def test_chat_request_capabilities(fields, expected):
    request = ChatCompletionRequest.model_validate({
        "model": "model-a", "messages": [{"role": "user", "content": "hello"}], **fields,
    })
    assert chat_required_capabilities(request) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}, {"function_call"}),
        ({"messages": [{"role": "assistant", "content": [{
            "type": "tool_use", "id": "tool-1", "name": "lookup", "input": {},
        }]}]}, {"function_call"}),
        ({"messages": [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
        }]}]}, {"function_call"}),
        ({"stream": True, "messages": [{"role": "user", "content": [{
            "type": "image", "source": {"type": "url", "url": "https://image.example/a.png"},
        }]}]}, {"stream", "vision"}),
        ({"thinking": {"type": "adaptive"}}, {"anthropic_native"}),
        ({"messages": [{"role": "user", "content": [{
            "type": "document", "source": {"type": "text", "data": "document body"},
        }]}]}, {"anthropic_native"}),
        ({"messages": [{"role": "assistant", "content": [{
            "type": "thinking", "thinking": "thought", "signature": "sig",
        }]}]}, {"anthropic_native"}),
        ({"messages": [{"role": "user", "content": [{
            "type": "future_native_block", "payload": "keep me",
        }]}]}, {"anthropic_native"}),
        ({"messages": [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tool-1", "content": [{
                "type": "image", "source": {"type": "url", "url": "https://image.example/a.png"},
            }],
        }]}]}, {"function_call", "vision", "anthropic_native"}),
    ],
)
def test_anthropic_request_capabilities(fields, expected):
    request = AnthropicMessageRequest.model_validate({
        "model": "model-a", "max_tokens": 128,
        "messages": [{"role": "user", "content": "hello"}], **fields,
    })
    assert anthropic_required_capabilities(request) == expected


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "openai_responses"])
def test_vision_respects_allow_list_and_lease(protocol):
    channels = [_channel(1, protocol, []), _channel(2, protocol, ["vision"])]
    decision = RoutingEngine().route(
        channels, "model-a", required_capabilities={"vision"}, preferred_channel_id=1,
    )
    assert [channel.id for channel in decision.candidates] == [2]
    assert not decision.lease_used


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_function_calls_respect_explicit_allow_list(protocol):
    channels = [_channel(1, protocol, []), _channel(2, protocol, ["function_call"])]
    decision = RoutingEngine().route(channels, "model-a", required_capabilities={"function_call"})
    assert [channel.id for channel in decision.candidates] == [2]


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "openai_responses"])
def test_missing_allow_list_preserves_compatibility(protocol):
    channel = _channel(1, protocol)
    assert RoutingEngine().route(
        [channel], "model-a", required_capabilities={"stream", "function_call", "vision"},
    ).candidates == [channel]


@pytest.mark.parametrize(
    ("required", "expected_ids"),
    [({"openai_chat_native"}, [1]), ({"anthropic_native"}, [2]), ({"reasoning_effort"}, [1, 3])],
)
def test_protocol_guards_precede_protocol_affinity_and_lease(required, expected_ids):
    channels = [_channel(1, "openai"), _channel(2, "anthropic"), _channel(3, "openai_responses")]
    decision = RoutingEngine(strategy="fallback_order").route(
        channels, "model-a", required_capabilities=required,
        preferred_channel_id=3 if "reasoning_effort" not in required else 2,
    )
    assert [channel.id for channel in decision.candidates] == expected_ids
    assert not decision.lease_used


def _patch_endpoint(monkeypatch, endpoint, channels):
    from rotor.services.session_leases import SessionLeasePreference
    monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=channels))
    monkeypatch.setattr(endpoint, "get_session_lease_preference", AsyncMock(return_value=SessionLeasePreference(1)))
    monkeypatch.setattr(endpoint, "routing_engine", RoutingEngine(strategy="fallback_order"))
    return Request({
        "type": "http", "method": "POST", "path": "/v1/messages",
        "headers": [], "client": ("127.0.0.1", 1),
    })


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("capability", ["function_call", "vision"])
def test_endpoint_rejects_unsupported_tools_or_images_before_upstream(monkeypatch, protocol, capability):
    endpoint = chat_endpoint if protocol == "openai" else anthropic_endpoint
    http_request = _patch_endpoint(monkeypatch, endpoint, [_channel(1, protocol, [])])
    create_client = Mock(side_effect=AssertionError("must not create an upstream client"))
    monkeypatch.setattr(endpoint, "AsyncClient", create_client)
    fields = {"model": "model-a", "messages": [{"role": "user", "content": "hello"}]}
    if capability == "function_call":
        if protocol == "openai":
            fields["messages"] = [{"role": "tool", "tool_call_id": "call-1", "content": "ok"}]
        else:
            fields["messages"][0]["content"] = [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]
    else:
        fields["messages"][0]["content"] = [
            {"type": "image_url", "image_url": {"url": "https://image.example/a.png"}}
            if protocol == "openai" else
            {"type": "image", "source": {"type": "url", "url": "https://image.example/a.png"}}
        ]
    request = ChatCompletionRequest.model_validate(fields) if protocol == "openai" else AnthropicMessageRequest.model_validate({**fields, "max_tokens": 128})
    handler = endpoint.chat_completions if protocol == "openai" else endpoint.messages
    with pytest.raises(ChannelException) as captured:
        asyncio.run(handler(request, http_request, Response(), db=SimpleNamespace(), token=SimpleNamespace(id=7)))
    assert captured.value.status_code == 503
    assert capability in captured.value.detail["error"]["message"]
    create_client.assert_not_called()


@pytest.mark.parametrize("native_protocol,provider_type", [("anthropic", "openai"), ("openai", "zhipu"), (None, "openai")])
def test_anthropic_native_semantics_reach_http_body_or_fail_without_upstream(monkeypatch, native_protocol, provider_type):
    channels = [_channel(1, "openai")]
    if native_protocol is not None:
        channels.append(_channel(2, native_protocol, provider_type=provider_type))
    http_request = _patch_endpoint(monkeypatch, anthropic_endpoint, channels)
    seen = []
    response_body = {
        "id": "msg-1", "type": "message", "role": "assistant", "model": "model-a",
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 1},
    }

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response_body)

    monkeypatch.setattr(anthropic_endpoint, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    accounting = SimpleNamespace(
        record_routing_decision=Mock(), record_success=AsyncMock(),
        extract_usage=AccountingService().extract_usage,
    )
    monkeypatch.setattr(anthropic_endpoint, "accounting_service", accounting)
    monkeypatch.setattr(anthropic_endpoint, "attempt_recorder", SimpleNamespace(record=AsyncMock()))
    monkeypatch.setattr(anthropic_endpoint, "conversation_store", SimpleNamespace(
        start=AsyncMock(return_value=SimpleNamespace(conversation_id="conv-1")),
        append_routing=AsyncMock(), append_response=AsyncMock(), append_usage=AsyncMock(), finish=AsyncMock(),
    ))
    fields = {
        "model": "model-a", "max_tokens": 128, "thinking": {"type": "adaptive"},
        "messages": [
            {"role": "user", "content": [{"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "the actual document"}}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "the prior thought", "signature": "sig-1"}]},
        ],
    }
    request = AnthropicMessageRequest.model_validate(fields)
    call = anthropic_endpoint.messages(request, http_request, Response(), db=SimpleNamespace(commit=AsyncMock()), token=SimpleNamespace(id=7))
    if native_protocol is None:
        with pytest.raises(ChannelException) as captured:
            asyncio.run(call)
        assert captured.value.status_code == 503
        assert "anthropic_native" in captured.value.detail["error"]["message"]
        assert seen == []
    else:
        assert asyncio.run(call) == response_body
        assert len(seen) == 1
        assert seen[0]["messages"] == fields["messages"]
        assert seen[0]["thinking"] == fields["thinking"]
