import asyncio
import json
from types import SimpleNamespace

from httpx import AsyncByteStream, AsyncClient, MockTransport, Request, Response
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.responses import (
    responses_request_to_chat,
    responses_required_capabilities,
)
from rotor.api.v1.responses import ResponsesStreamTransform, chat_response_to_response
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
                        "status": "completed",
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
        "function_call",
        "responses_native",
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
