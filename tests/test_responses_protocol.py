import asyncio
import json
from types import SimpleNamespace

from httpx import (
    AsyncByteStream,
    AsyncClient,
    HTTPStatusError,
    MockTransport,
    Request,
    Response,
)
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.responses import (
    responses_request_to_chat,
    responses_required_capabilities,
)
from rotor.core.exceptions import UpstreamOverloaded
from rotor.api.v1.responses import ResponsesStreamTransform, chat_response_to_response
from rotor.api.v1.anthropic import anthropic_to_openai_request
from rotor.schemas.request import (
    AnthropicMessageRequest,
    ChatCompletionRequest,
    ChatMessage,
)
from rotor.schemas.responses import ResponsesRequest
from rotor.gateway.accounting import AccountingService


def test_chat_response_converts_to_responses_shape() -> None:
    response = chat_response_to_response({
        "id": "chatcmpl_1",
        "created": 123,
        "model": "test-model",
        "choices": [{
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        },
    })

    assert response["object"] == "response"
    assert response["status"] == "completed"
    assert response["output"][0]["content"][0]["text"] == "hello"
    assert response["usage"]["input_tokens"] == 4
    assert response["usage"]["output_tokens"] == 2
    assert response["usage"]["total_tokens"] == 6


def test_chat_stream_converts_to_responses_event_sequence() -> None:
    transform = ResponsesStreamTransform("test-model")

    events = transform.start()
    events += transform.feed({
        "choices": [{
            "delta": {"content": "hel"},
            "finish_reason": None,
        }],
    })
    events += transform.feed({
        "choices": [{
            "delta": {"content": "lo"},
            "finish_reason": None,
        }],
    })
    events += transform.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    events += transform.finish({
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    })

    event_types = [event["type"] for event in events]
    assert event_types == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "hello"
    assert events[-1]["response"]["usage"]["total_tokens"] == 6


def test_responses_function_call_and_output_convert_to_chat() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "tools": [{
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            "strict": True,
        }],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "input": [
            {"role": "user", "content": "Weather?"},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city":"Shanghai"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": {"temperature": 30},
            },
        ],
    })

    converted = responses_request_to_chat(request)

    assert converted.tools[0].function.name == "get_weather"
    assert converted.tools[0].function.strict is True
    assert converted.tool_choice == {
        "type": "function",
        "function": {"name": "get_weather"},
    }
    assert converted.messages[1].tool_calls[0].id == "call_1"
    assert json.loads(converted.messages[1].tool_calls[0].function.arguments) == {
        "city": "Shanghai"
    }
    assert converted.messages[2].tool_call_id == "call_1"
    assert json.loads(converted.messages[2].content) == {"temperature": 30}
    assert converted.responses_payload["tools"][0]["strict"] is True


def test_chat_tool_calls_convert_to_responses_output_items() -> None:
    response = chat_response_to_response({
        "id": "chatcmpl_tool",
        "created": 123,
        "model": "test-model",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Shanghai"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })

    assert response["output"] == [{
        "id": response["output"][0]["id"],
        "type": "function_call",
        "status": "completed",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"Shanghai"}',
    }]


def test_chat_tool_stream_converts_to_responses_function_events() -> None:
    transform = ResponsesStreamTransform("test-model")
    events = transform.start()
    events += transform.feed({
        "choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":'},
            }]},
            "finish_reason": None,
        }],
    })
    events += transform.feed({
        "choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": '"Shanghai"}'},
            }]},
            "finish_reason": None,
        }],
    })
    events += transform.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    events += transform.finish({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    event_types = [event["type"] for event in events]
    assert event_types.count("response.function_call_arguments.delta") == 2
    assert "response.function_call_arguments.done" in event_types
    assert events[-1]["response"]["output"] == [{
        "id": events[-1]["response"]["output"][0]["id"],
        "type": "function_call",
        "status": "completed",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"Shanghai"}',
    }]


def test_responses_protocol_channel_uses_native_adapter_and_payload() -> None:
    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={"public-model": "provider-model"},
    )
    request = ResponsesRequest.model_validate({
        "model": "public-model",
        "input": "hello",
        "tools": [{"type": "web_search"}],
        "store": False,
    })
    chat_request = responses_request_to_chat(request)
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    body = asyncio.run(adapter.convert_request(chat_request))
    url = asyncio.run(adapter.get_request_url(chat_request))

    assert type(adapter).__name__ == "OpenAIResponsesAdapter"
    assert url == "https://example.com/v1/responses"
    assert body["model"] == "provider-model"
    assert body["tools"] == [{"type": "web_search"}]
    assert body["store"] is False


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort_fields", [
    {},
    {"reasoning_effort": None},
    *[
        {"reasoning_effort": effort}
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "future-effort")
    ],
])
def test_chat_reasoning_effort_reaches_responses_upstream(effort_fields, stream) -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        assert request.url.path == "/v1/responses"
        bodies.append(json.loads(request.content))
        if stream:
            return Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"type":"response.completed"}\n\n',
            )
        return Response(200, json={"id": "resp_1", "status": "completed"})

    request = ChatCompletionRequest.model_validate({
        "model": "public-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
        **effort_fields,
    })
    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={"public-model": "provider-model"},
    )

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            response = await adapter.make_request(request)
            try:
                await response.aread()
            finally:
                await response.aclose()

    asyncio.run(exercise())

    assert len(bodies) == 1
    assert bodies[0]["model"] == "provider-model"
    assert bodies[0]["stream"] is stream
    assert "reasoning_effort" not in bodies[0]
    effort = effort_fields.get("reasoning_effort")
    if effort is None:
        assert "reasoning" not in bodies[0]
    else:
        assert bodies[0]["reasoning"] == {"effort": effort}


@pytest.mark.parametrize("stream", [False, True])
def test_native_responses_reasoning_options_are_preserved(stream) -> None:
    reasoning = {"effort": "high", "summary": "auto"}
    request = responses_request_to_chat(ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": stream,
        "reasoning": reasoning,
    }))
    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    adapter = AdapterFactory.create_adapter(channel, http_client=None)

    body = asyncio.run(adapter.convert_request(request))

    assert body["reasoning"] == reasoning
    assert body["stream"] is stream
    assert "reasoning_effort" not in body


def test_chat_conversion_drops_tool_choice_when_no_function_tools_remain() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "tools": [
            {"type": "web_search"},
            {"type": "namespace", "name": "multi_agent_v1", "tools": []},
        ],
        "tool_choice": "auto",
    })

    converted = responses_request_to_chat(request)

    assert converted.tools is None
    assert converted.tool_choice is None


def test_native_responses_retries_without_unsupported_cache_retention() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return Response(400, json={
                "error": {
                    "message": (
                        "prompt_cache_retention is not supported on this model"
                    ),
                    "type": "invalid_request_error",
                    "param": "prompt_cache_retention",
                    "code": "invalid_parameter",
                }
            })
        return Response(200, json={"id": "resp_1", "status": "completed"})

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = responses_request_to_chat(ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "prompt_cache_key": "session-1",
        "prompt_cache_retention": "24h",
    }))

    async def exercise() -> Response:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            return await adapter.make_request(request)

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert bodies[0]["prompt_cache_retention"] == "24h"
    assert "prompt_cache_retention" not in bodies[1]
    assert bodies[1]["prompt_cache_key"] == "session-1"
    assert request.responses_payload["prompt_cache_retention"] == "24h"


def test_native_responses_stream_retries_unsupported_cache_retention() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return Response(400, json={
                "error": {
                    "message": (
                        "prompt_cache_retention is not supported on this model"
                    ),
                    "type": "invalid_request_error",
                    "param": "prompt_cache_retention",
                    "code": "invalid_parameter",
                }
            })
        return Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type":"response.completed"}\n\n',
        )

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = responses_request_to_chat(ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "prompt_cache_retention": "24h",
    }))

    async def exercise() -> bytes:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            response = await adapter.make_request(request)
            try:
                return await response.aread()
            finally:
                await response.aclose()

    content = asyncio.run(exercise())

    assert b"response.completed" in content
    assert len(bodies) == 2
    assert "prompt_cache_retention" not in bodies[1]


def test_native_responses_does_not_retry_other_invalid_parameter() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        return Response(400, json={
            "error": {
                "message": "temperature is not supported on this model",
                "type": "invalid_request_error",
                "param": "temperature",
                "code": "invalid_parameter",
            }
        })

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = responses_request_to_chat(ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "prompt_cache_retention": "24h",
    }))

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            with pytest.raises(HTTPStatusError):
                await adapter.make_request(request)

    asyncio.run(exercise())

    assert len(bodies) == 1


def _responses_cache_breakpoint_channel() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )


def _anthropic_request_with_responses_breakpoint() -> ChatCompletionRequest:
    return anthropic_to_openai_request(AnthropicMessageRequest.model_validate({
        "model": "gpt-5.6-sol",
        "max_tokens": 64,
        "system": [{
            "type": "text",
            "text": "Cached instructions",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": "hello"}],
    }))


def test_responses_retries_anthropic_breakpoint_without_nested_field() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return Response(400, json={
                "error": {
                    "message": "prompt_cache_breakpoint is not supported on this model",
                    "code": "invalid_parameter",
                }
            })
        return Response(200, json={"id": "resp_1", "status": "completed"})

    request = _anthropic_request_with_responses_breakpoint()
    request.responses_prompt_cache_key = "anthropic-cache-key"

    async def exercise() -> Response:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(
                _responses_cache_breakpoint_channel(), client
            )
            return await adapter.make_request(request)

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert "prompt_cache_breakpoint" in json.dumps(bodies[0])
    assert "prompt_cache_breakpoint" not in json.dumps(bodies[1])
    assert bodies[1]["prompt_cache_key"] == "anthropic-cache-key"
    assert bodies[1]["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert len(bodies) == 2
    assert request.responses_cacheable_system_content == [{
        "type": "input_text",
        "text": "Cached instructions",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }]


def test_responses_stream_retries_anthropic_breakpoint_without_nested_field() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return Response(400, json={
                "error": {
                    "param": "prompt_cache_breakpoint",
                    "code": "invalid_parameter",
                }
            })
        return Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type":"response.completed"}\n\n',
        )

    request = _anthropic_request_with_responses_breakpoint()
    request.stream = True

    async def exercise() -> bytes:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(
                _responses_cache_breakpoint_channel(), client
            )
            response = await adapter.make_request(request)
            try:
                return await response.aread()
            finally:
                await response.aclose()

    content = asyncio.run(exercise())

    assert b"response.completed" in content
    assert len(bodies) == 2
    assert "prompt_cache_breakpoint" not in json.dumps(bodies[1])


def test_responses_breakpoint_retry_requires_invalid_parameter_error() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        return Response(400, json={
            "error": {
                "message": "prompt_cache_breakpoint is not supported on this model",
                "code": "unsupported_parameter",
            }
        })

    request = _anthropic_request_with_responses_breakpoint()

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(
                _responses_cache_breakpoint_channel(), client
            )
            with pytest.raises(HTTPStatusError):
                await adapter.make_request(request)

    asyncio.run(exercise())

    assert len(bodies) == 1


def test_responses_breakpoint_retry_rejects_inconsistent_error_message() -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        return Response(400, json={
            "error": {
                "param": "prompt_cache_breakpoint",
                "message": "temperature is not supported on this model",
                "code": "invalid_parameter",
            }
        })

    request = _anthropic_request_with_responses_breakpoint()

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(
                _responses_cache_breakpoint_channel(), client
            )
            with pytest.raises(HTTPStatusError):
                await adapter.make_request(request)

    asyncio.run(exercise())

    assert len(bodies) == 1


def test_native_responses_resource_methods_preserve_path_query_and_model_mapping() -> None:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        return Response(200, json={"ok": True})

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={"request_path": "/custom/responses"},
        model_mapping={"public-model": "provider-model"},
    )

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            await adapter.retrieve_response(
                "resp/a",
                params=[("include", "reasoning.encrypted_content"), ("stream", "false")],
            )
            await adapter.cancel_response("resp/a")
            await adapter.list_input_items("resp/a", params=[("limit", "10")])
            await adapter.delete_response("resp/a")
            await adapter.count_input_tokens({"model": "public-model", "input": "hi"})
            await adapter.compact_response({"model": "public-model", "input": []})

    asyncio.run(exercise())

    assert [request.method for request in seen] == [
        "GET", "POST", "GET", "DELETE", "POST", "POST"
    ]
    assert seen[0].url.raw_path.split(b"?", 1)[0] == b"/v1/custom/responses/resp%2Fa"
    assert seen[0].url.params.get_list("include") == ["reasoning.encrypted_content"]
    assert seen[1].url.raw_path == b"/v1/custom/responses/resp%2Fa/cancel"
    assert seen[2].url.path == "/v1/custom/responses/resp/a/input_items"
    assert seen[4].url.path == "/v1/custom/responses/input_tokens"
    assert json.loads(seen[4].content)["model"] == "provider-model"
    assert seen[5].url.path == "/v1/custom/responses/compact"


def test_native_responses_non_streaming_response_is_not_downgraded() -> None:
    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
    })
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    native = {
        "id": "resp_1",
        "object": "response",
        "created_at": 123,
        "status": "completed",
        "model": "test-model",
        "output": [{
            "id": "fc_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
        }],
    }
    response = Response(
        200,
        json=native,
        request=Request("POST", "https://example.com/v1/responses"),
    )

    converted = asyncio.run(adapter.convert_response(
        response,
        responses_request_to_chat(request),
    ))

    assert converted == native


def test_native_responses_stream_events_are_forwarded_without_downgrade() -> None:
    class EventStream(AsyncByteStream):
        async def __aiter__(self):
            events = [
                {"type": "response.created", "response": {"id": "resp_1"}},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "get_weather",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": "{}",
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "object": "response",
                        "status": "completed",
                        "output": [{
                            "id": "fc_1",
                            "type": "function_call",
                            "status": "completed",
                            "call_id": "call_1",
                            "name": "get_weather",
                            "arguments": "{}",
                        }],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                },
            ]
            yield "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
    })
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=EventStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(
                response,
                responses_request_to_chat(request),
            )
        ]

    events = asyncio.run(collect())

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.completed",
    ]
    assert events[-1]["response"]["usage"]["total_tokens"] == 15


def test_cross_protocol_responses_stream_preserves_cache_usage_details() -> None:
    class EventStream(AsyncByteStream):
        async def __aiter__(self):
            event = {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {
                            "cached_tokens": 60,
                            "cache_write_tokens": 30,
                        },
                        "output_tokens": 5,
                        "output_tokens_details": {"reasoning_tokens": 3},
                        "total_tokens": 105,
                    },
                },
            }
            yield (
                f"event: response.completed\ndata: {json.dumps(event)}\n\n"
            ).encode()

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=EventStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )
    request = ChatCompletionRequest(
        model="gpt-5.6-sol",
        messages=[ChatMessage(role="user", content="hello")],
        stream=True,
    )

    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(response, request)
        ]

    events = asyncio.run(collect())

    assert events[-1]["usage"]["prompt_tokens_details"] == {
        "cached_tokens": 60,
        "cache_write_tokens": 30,
    }
    assert events[-1]["usage"]["completion_tokens_details"] == {
        "reasoning_tokens": 3,
    }


def _collect_stream_events(adapter, response, request):
    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(
                response,
                responses_request_to_chat(request),
            )
        ]

    return asyncio.run(collect())


def _collect_direct_stream_events(adapter, response, request):
    """Collect a stream without converting a Responses request first."""
    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(response, request)
        ]

    return asyncio.run(collect())


def _responses_test_channel(**overrides):
    values = {
        "id": 1,
        "name": "responses-test",
        "type": "openai",
        "protocol": "openai_responses",
        "base_url": "https://example.com/v1",
        "key": "secret",
        "extra": {},
        "model_mapping": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _chat_stream_request(model: str = "test-model") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hello")],
        stream=True,
    )


def test_chat_to_responses_top_level_error_preserves_message_and_code() -> None:
    class ErrorStream(AsyncByteStream):
        async def __aiter__(self):
            yield (
                b"event: error\n"
                b'data: {"type":"error","code":"server_error",'
                b'"message":"provider said no","param":null}\n\n'
            )

    channel = _responses_test_channel(
        model_mapping={"test-model": "provider-model"},
    )
    request = _chat_stream_request()
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=ErrorStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    # Confirm this is the Chat -> Responses conversion path, rather than a
    # native Responses request carrying responses_payload.
    converted = asyncio.run(adapter.convert_request(request))
    assert converted["model"] == "provider-model"
    assert converted["stream"] is True
    assert request.responses_payload is None

    with pytest.raises(RuntimeError) as captured:
        _collect_direct_stream_events(adapter, response, request)

    assert str(captured.value) == "provider said no"
    assert captured.value.error_type == "server_error"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("rate_limit_error", "try again after rate limiting"),
        ("overloaded_error", "the upstream is overloaded"),
    ],
)
def test_chat_to_responses_top_level_overload_codes_raise_upstream_overloaded(
    code: str,
    message: str,
) -> None:
    class ErrorStream(AsyncByteStream):
        async def __aiter__(self):
            payload = json.dumps({
                "type": "error",
                "code": code,
                "message": message,
                "param": None,
            })
            yield f"event: error\ndata: {payload}\n\n".encode()

    adapter = AdapterFactory.create_adapter(
        _responses_test_channel(),
        http_client=None,
    )
    response = Response(
        200,
        stream=ErrorStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    with pytest.raises(UpstreamOverloaded) as captured:
        _collect_direct_stream_events(adapter, response, _chat_stream_request())

    assert str(captured.value) == message
    assert captured.value.error_type == code


def test_responses_stream_overload_error_raises_upstream_overloaded() -> None:
    class OverloadedStream(AsyncByteStream):
        async def __aiter__(self):
            events = [
                {"type": "response.created", "response": {"id": "resp_1"}},
                {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Our servers are currently overloaded.",
                    },
                },
            ]
            yield "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
    })
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=OverloadedStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    with pytest.raises(UpstreamOverloaded) as captured:
        _collect_stream_events(adapter, response, request)

    assert captured.value.error_type == "overloaded_error"


def test_responses_stream_overload_by_wording_raises_upstream_overloaded() -> None:
    class OverloadedStream(AsyncByteStream):
        async def __aiter__(self):
            events = [{
                "type": "response.failed",
                "response": {
                    "id": "resp_1",
                    "error": {"message": "Upstream is overloaded, retry later"},
                },
            }]
            yield "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
    })
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=OverloadedStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    with pytest.raises(UpstreamOverloaded):
        _collect_stream_events(adapter, response, request)


def test_responses_stream_parameter_error_still_raises_runtime_error() -> None:
    class InvalidRequestStream(AsyncByteStream):
        async def __aiter__(self):
            events = [{
                "type": "error",
                "error": {
                    "code": "invalid_request_error",
                    "message": "max_tokens too large",
                },
            }]
            yield "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()

    channel = SimpleNamespace(
        id=1,
        type="openai",
        protocol="openai_responses",
        base_url="https://example.com/v1",
        key="secret",
        extra={},
        model_mapping={},
    )
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
    })
    adapter = AdapterFactory.create_adapter(channel, http_client=None)
    response = Response(
        200,
        stream=InvalidRequestStream(),
        request=Request("POST", "https://example.com/v1/responses"),
    )

    with pytest.raises(RuntimeError) as captured:
        _collect_stream_events(adapter, response, request)

    assert not isinstance(captured.value, UpstreamOverloaded)


def test_native_only_responses_features_require_native_channel() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "role": "user",
            "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}],
        }],
        "tools": [{"type": "web_search"}],
        "previous_response_id": "resp_previous",
        "stream": True,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
        "vision",
    }


def test_codex_optional_tools_can_fall_back_to_chat_channel() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }],
        "stream": True,
        "store": False,
        "include": [],
        "reasoning": None,
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [{
                    "type": "function",
                    "name": "spawn_agent",
                    "parameters": {"type": "object", "properties": {}},
                }],
            },
            {"type": "web_search"},
        ],
        "client_metadata": {"originator": "codex_cli_rs"},
    })

    converted = responses_request_to_chat(request)

    assert responses_required_capabilities(request) == {"stream", "function_call"}
    assert [tool.function.name for tool in converted.tools] == ["exec_command"]
    assert converted.responses_payload["tools"] == request.tools


@pytest.mark.parametrize(
    "tools",
    [
        [{"type": "web_search"}],
        [{"type": "namespace", "name": "shell", "tools": []}],
    ],
)
def test_hosted_or_namespace_only_tools_can_fall_back_to_chat_channel(tools) -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "tools": tools,
    })

    converted = responses_request_to_chat(request)

    # These tool declarations are preserved for a native Responses upstream,
    # but none has a Chat Completions representation. They must not make a
    # Chat channel look as if it needs function-call capability.
    assert responses_required_capabilities(request) == {"stream"}
    assert converted.tools is None


def test_hosted_tool_required_choice_remains_native_only() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "tools": [{"type": "web_search"}],
        "tool_choice": "required",
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
    }


@pytest.mark.parametrize(
    "tool_choice",
    [
        {"type": "namespace", "name": "shell"},
        {"type": "function", "name": "missing"},
        "required",
    ],
)
def test_mixed_tools_with_unrepresentable_choice_remain_native_only(tool_choice) -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object"},
            },
            {"type": "namespace", "name": "shell", "tools": []},
        ],
        "tool_choice": tool_choice,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "function_call",
        "responses_native",
    }


def test_mixed_tools_with_valid_function_choice_can_fall_back_to_chat() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object"},
            },
            {"type": "namespace", "name": "shell", "tools": []},
        ],
        "tool_choice": {"type": "function", "name": "exec_command"},
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "function_call",
    }


def test_responses_image_url_can_fall_back_to_chat_channel() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe this"},
                {
                    "type": "input_image",
                    "image_url": "https://example.com/image.png",
                    "detail": "low",
                },
            ],
        }],
        "stream": True,
    })

    converted = responses_request_to_chat(request)

    assert responses_required_capabilities(request) == {"stream", "vision"}
    assert converted.messages[0].content == [
        {"type": "text", "text": "describe this"},
        {
            "type": "image_url",
            "image_url": {
                "url": "https://example.com/image.png",
                "detail": "low",
            },
        },
    ]


def test_responses_file_backed_image_remains_native_only() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "file_id": "file_123"}],
        }],
        "stream": True,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
        "vision",
    }


def test_responses_native_items_remain_native_only_without_state_id() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "type": "reasoning",
            "summary": [],
        }],
        "stream": True,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
    }


def test_unrecognized_response_content_block_remains_native_only() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": ["provider-native-block"],
        }],
        "stream": True,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
    }


def test_responses_reasoning_options_remain_native_only() -> None:
    request = ResponsesRequest.model_validate({
        "model": "test-model",
        "input": "hello",
        "reasoning": {"effort": "medium"},
        "stream": True,
    })

    assert responses_required_capabilities(request) == {
        "stream",
        "responses_native",
    }


def test_responses_include_requires_native_only_when_non_empty() -> None:
    for include, expected in (
        ([], {"stream"}),
        (["reasoning.encrypted_content"], {"stream", "responses_native"}),
    ):
        request = ResponsesRequest.model_validate({
            "model": "test-model",
            "input": "hello",
            "include": include,
            "stream": True,
        })

        assert responses_required_capabilities(request) == expected


def test_responses_usage_details_are_accounted() -> None:
    usage = AccountingService().extract_usage({
        "usage": {
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 8, "audio_tokens": 2},
            "output_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": 4, "audio_tokens": 1},
            "total_tokens": 30,
        },
    })

    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 10
    assert usage.cached_tokens == 8
    assert usage.reasoning_tokens == 4
    assert usage.input_audio_tokens == 2
    assert usage.output_audio_tokens == 1


def test_converted_function_response_parses_with_openai_sdk() -> None:
    response_module = pytest.importorskip("openai.types.responses.response")
    payload = chat_response_to_response({
        "id": "chatcmpl_1",
        "created": 123,
        "model": "test-model",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "health", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    })

    parsed = response_module.Response.model_validate(payload)

    assert parsed.output[0].type == "function_call"
    assert parsed.output[0].call_id == "call_1"


def test_converted_function_stream_events_parse_with_openai_sdk() -> None:
    pytest.importorskip("openai")
    from openai.types.responses.response_completed_event import ResponseCompletedEvent
    from openai.types.responses.response_created_event import ResponseCreatedEvent
    from openai.types.responses.response_function_call_arguments_delta_event import (
        ResponseFunctionCallArgumentsDeltaEvent,
    )
    from openai.types.responses.response_function_call_arguments_done_event import (
        ResponseFunctionCallArgumentsDoneEvent,
    )
    from openai.types.responses.response_in_progress_event import ResponseInProgressEvent
    from openai.types.responses.response_output_item_added_event import (
        ResponseOutputItemAddedEvent,
    )
    from openai.types.responses.response_output_item_done_event import (
        ResponseOutputItemDoneEvent,
    )

    transform = ResponsesStreamTransform("test-model")
    events = transform.start()
    events += transform.feed({
        "choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "health", "arguments": "{}"},
            }]},
            "finish_reason": None,
        }],
    })
    events += transform.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    events += transform.finish({"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
    event_models = {
        "response.created": ResponseCreatedEvent,
        "response.in_progress": ResponseInProgressEvent,
        "response.output_item.added": ResponseOutputItemAddedEvent,
        "response.function_call_arguments.delta": (
            ResponseFunctionCallArgumentsDeltaEvent
        ),
        "response.function_call_arguments.done": (
            ResponseFunctionCallArgumentsDoneEvent
        ),
        "response.output_item.done": ResponseOutputItemDoneEvent,
        "response.completed": ResponseCompletedEvent,
    }

    for event in events:
        event_models[event["type"]].model_validate(event)
