import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.adapters.protocol.responses import chat_request_to_responses_payload
from rotor.api.v1.anthropic import (
    OpenAIToAnthropicStreamConverter,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from rotor.core.exceptions import UpstreamProtocolError
from rotor.gateway.capabilities import anthropic_required_capabilities
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.request import AnthropicMessageRequest, AnthropicMessageResponse, ChatCompletionRequest


def channel(protocol="openai", channel_id=1, provider_type="openai"):
    return SimpleNamespace(
        id=channel_id, name=f"channel-{channel_id}", type=provider_type,
        protocol=protocol, enabled=True, priority=10, weight=1,
        extra={}, model_mapping={"model": "provider-model"},
        base_url="https://provider.example/v1", key="mock-key",
    )


def anthropic_request(**fields):
    return AnthropicMessageRequest.model_validate({
        "model": "model", "max_tokens": 32,
        "messages": [{"role": "user", "content": "hello"}], **fields,
    })


@pytest.mark.parametrize("protocol", ["responses", "openai_responses"])
def test_stop_sequences_exclude_responses_even_when_configured_or_leased(protocol):
    request = anthropic_request(stop_sequences=["END"])
    required = anthropic_required_capabilities(request)
    assert required == {"stop_sequences"}
    internal = anthropic_to_openai_request(request)
    assert internal.stop == ["END"]
    assert "stop" not in chat_request_to_responses_payload(internal)
    candidates = [channel(protocol), channel("openai", 2), channel("anthropic", 3)]
    candidates[0].extra = {"capabilities": ["stop_sequences"]}
    decision = RoutingEngine("fallback_order").route(
        candidates, "model", required_capabilities=required, preferred_channel_id=1,
    )
    assert {item.id for item in decision.candidates} == {2, 3}
    assert not decision.lease_used


@pytest.mark.parametrize("protocol,provider_type", [("openai", "openai"), ("anthropic", "openai"), ("openai", "zhipu")])
def test_stop_sequences_reach_supported_upstream_http(protocol, provider_type):
    request = anthropic_to_openai_request(anthropic_request(stop_sequences=["END"]))
    seen = []

    def upstream(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, json={})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter = AdapterFactory.create_adapter(channel(protocol, provider_type=provider_type), client)
            await adapter.make_request(request)

    asyncio.run(exercise())
    field = "stop_sequences" if protocol == "anthropic" or provider_type == "zhipu" else "stop"
    assert seen[0][field] == ["END"]
    assert seen[0]["model"] == "provider-model"


@pytest.mark.parametrize("stop_sequences", [None, []])
def test_absent_stop_sequences_do_not_block_responses(stop_sequences):
    assert anthropic_required_capabilities(anthropic_request(stop_sequences=stop_sequences)) == set()


def test_anthropic_thinking_is_separate_from_text_nonstream_and_stream():
    native = AnthropicMessageResponse.model_validate({
        "id": "msg-1", "type": "message", "role": "assistant", "model": "provider-model",
        "content": [{"type": "thinking", "thinking": "reason", "signature": "native-signature"},
                    {"type": "text", "text": "answer"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2},
    })
    response = ProtocolConverter.anthropic_to_openai(native, "model")
    assert response["choices"][0]["message"] == {
        "role": "assistant", "content": "answer", "reasoning_content": "reason",
    }
    chunk = ProtocolConverter.anthropic_stream_to_openai({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "reason"},
    }, "model", "chunk-1")
    assert chunk["choices"][0]["delta"] == {"reasoning_content": "reason"}


def test_redacted_thinking_fails_cross_protocol_but_native_response_is_preserved():
    payload = {
        "id": "msg-1", "type": "message", "role": "assistant", "model": "model",
        "content": [{"type": "redacted_thinking", "data": "opaque-data"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    with pytest.raises(UpstreamProtocolError, match="Redacted"):
        ProtocolConverter.anthropic_to_openai(AnthropicMessageResponse.model_validate(payload), "model")
    with pytest.raises(UpstreamProtocolError, match="Redacted"):
        ProtocolConverter.anthropic_stream_to_openai({
            "type": "content_block_start", "index": 0, "content_block": payload["content"][0],
        }, "model", "chunk-1")

    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("anthropic"), client)
            assert await adapter.convert_response(
                httpx.Response(200, json=payload), anthropic_to_openai_request(anthropic_request())
            ) == payload
    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["reasoning_content", "refusal"])
def test_chat_unrepresentable_content_fails_nonstream_and_stream(field):
    message = {"content": "answer", field: "not ordinary answer text"}
    with pytest.raises(UpstreamProtocolError, match="reasoning/refusal"):
        openai_to_anthropic_response({"choices": [{"message": message, "finish_reason": "stop"}]}, "model")
    converter = OpenAIToAnthropicStreamConverter()
    with pytest.raises(UpstreamProtocolError, match="reasoning/refusal"):
        converter.feed({"choices": [{"delta": message, "finish_reason": None}]})
    with pytest.raises(UpstreamProtocolError, match="failed"):
        converter.finish("stop")


@pytest.mark.parametrize("payload", [{"error": {"message": "secret input"}}, {"success": False}])
def test_chat_error_cannot_become_empty_success(payload):
    with pytest.raises(UpstreamProtocolError) as captured:
        openai_to_anthropic_response(payload, "model")
    assert "secret input" not in str(captured.value)
    converter = OpenAIToAnthropicStreamConverter()
    with pytest.raises(UpstreamProtocolError):
        converter.feed(payload)
    with pytest.raises(UpstreamProtocolError):
        converter.finish("stop")


@pytest.mark.parametrize("finish_reason", [None, "content_filter", "future_reason"])
def test_unrepresentable_terminal_reason_is_not_fabricated(finish_reason):
    with pytest.raises(UpstreamProtocolError):
        openai_to_anthropic_response({"choices": [{"message": {}, "finish_reason": finish_reason}]}, "model")
    with pytest.raises(UpstreamProtocolError):
        OpenAIToAnthropicStreamConverter().finish(finish_reason)


@pytest.mark.parametrize("finish_reason,expected", [("stop", "end_turn"), ("length", "max_tokens"), ("tool_calls", "tool_use")])
def test_representable_terminal_reasons_remain_supported(finish_reason, expected):
    assert OpenAIToAnthropicStreamConverter().finish(finish_reason)[-2]["delta"]["stop_reason"] == expected


@pytest.mark.parametrize("payload", [
    {},
    {"code": 500, "msg": "upstream failure"},
    {"status": "completed"},
    {"status": "completed", "output": {}},
    {"status": "completed", "output": None},
    {"type": "error", "status": "completed", "output": []},
    {"status": "incomplete", "output": []},
    {"status": "failed", "output": []},
    {"error": {"message": "failure"}},
])
def test_invalid_responses_nonstream_rejected_for_anthropic_and_chat(payload):
    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            with pytest.raises(UpstreamProtocolError):
                await adapter.convert_response(httpx.Response(200, json=payload), anthropic_to_openai_request(anthropic_request()))
            with pytest.raises(UpstreamProtocolError):
                await adapter.convert_response(httpx.Response(200, json=payload), ChatCompletionRequest(model="model", messages=[]))
    asyncio.run(exercise())


@pytest.mark.parametrize("output", [
    [{"type": "reasoning", "summary": []}],
    [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
])
def test_responses_unrepresentable_content_remains_rejected_only_for_anthropic(output):
    payload = {"id": "resp_1", "object": "response", "status": "completed", "output": output}

    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            with pytest.raises(UpstreamProtocolError):
                await adapter.convert_response(httpx.Response(200, json=payload), anthropic_to_openai_request(anthropic_request()))
            # This batch does not add Chat reasoning/refusal mappings.
            result = await adapter.convert_response(httpx.Response(200, json=payload), ChatCompletionRequest(model="model", messages=[]))
            assert "choices" in result
    asyncio.run(exercise())


@pytest.mark.parametrize("event", [
    {"type": "response.completed"},
    {"type": "response.completed", "response": {}},
    {"type": "response.incomplete", "response": {"id": "resp_1", "object": "response", "status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}},
    {"type": "response.reasoning_text.delta", "delta": "reason"},
    {"type": "response.reasoning_summary_text.delta", "delta": "reason"},
    {"type": "response.refusal.delta", "delta": "no"},
    {"type": "response.output_item.added", "item": {"type": "reasoning"}},
    {"type": "response.content_part.added", "part": {"type": "refusal", "refusal": "no"}},
    {"type": "response.completed", "response": {"id": "resp_1", "object": "response", "status": "completed", "output": [{"type": "reasoning"}]}},
])
def test_responses_unrepresentable_stream_fails_before_synthetic_success(event):
    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            response = httpx.Response(200, content=f"data: {json.dumps(event)}\n\n")
            with pytest.raises(UpstreamProtocolError):
                _ = [chunk async for chunk in adapter.stream_convert_response(response, anthropic_to_openai_request(anthropic_request()))]
    asyncio.run(exercise())


@pytest.mark.parametrize("bad_data", ["{not json", "[]", "null"])
def test_malformed_responses_sse_cannot_be_hidden_by_later_completed_event(bad_data):
    completed = {"type": "response.completed", "response": {"id": "resp_1", "object": "response", "status": "completed", "output": []}}
    body = f"data: {bad_data}\n\ndata: {json.dumps(completed)}\n\n"

    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            with pytest.raises(UpstreamProtocolError):
                _ = [chunk async for chunk in adapter.stream_convert_response(
                    httpx.Response(200, content=body), anthropic_to_openai_request(anthropic_request())
                )]
            with pytest.raises(UpstreamProtocolError):
                _ = [chunk async for chunk in adapter.stream_convert_response(
                    httpx.Response(200, content=body), ChatCompletionRequest(model="model", messages=[])
                )]
    asyncio.run(exercise())


def test_real_completed_empty_responses_output_remains_valid():
    payload = {"id": "resp_1", "object": "response", "status": "completed", "output": []}

    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            request = anthropic_to_openai_request(anthropic_request())
            chat = await adapter.convert_response(httpx.Response(200, json=payload), request)
            assert openai_to_anthropic_response(chat, "model")["stop_reason"] == "end_turn"
            body = f"data: {json.dumps({'type': 'response.completed', 'response': payload})}\n\n"
            chunks = [chunk async for chunk in adapter.stream_convert_response(httpx.Response(200, content=body), request)]
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    asyncio.run(exercise())


def test_responses_text_usage_and_tools_still_convert_for_anthropic():
    payload = {"id": "resp_1", "object": "response", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
    ], "usage": {"input_tokens": 8, "output_tokens": 2}}

    async def exercise():
        async with httpx.AsyncClient() as client:
            adapter = AdapterFactory.create_adapter(channel("openai_responses"), client)
            chat = await adapter.convert_response(httpx.Response(200, json=payload), anthropic_to_openai_request(anthropic_request()))
            converted = openai_to_anthropic_response(chat, "model")
            assert converted["content"] == [{"type": "text", "text": "answer"},
                                             {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}}]
            assert converted["stop_reason"] == "tool_use"
            assert converted["usage"]["input_tokens"] == 8
    asyncio.run(exercise())
