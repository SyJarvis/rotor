import json
import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncByteStream, AsyncClient, MockTransport, Request, Response
from pydantic import ValidationError

from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.adapters.protocol.anthropic import AnthropicAdapter
from rotor.adapters.protocol.openai import OpenAIAdapter
from rotor.adapters.providers.kimi import KimiAdapter
from rotor.adapters.providers.minimax import MiniMaxAdapter
from rotor.adapters.providers.moonshot import MoonshotAdapter
from rotor.adapters.providers.zhipu import ZhipuAdapter
from rotor.api.v1.anthropic import (
    OpenAIToAnthropicStreamConverter,
    accounting_service,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from rotor.schemas.request import AnthropicMessageRequest


def test_anthropic_request_moves_system_messages_to_top_level() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "glm-5.2",
        "max_tokens": 256,
        "system": "Base instructions.",
        "messages": [
            {"role": "user", "content": "Earlier question"},
            {
                "role": "system",
                "content": [{"type": "text", "text": "Injected reminder."}],
            },
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "system", "content": "Latest reminder."},
            {"role": "user", "content": "你好"},
        ],
    })

    assert [message.role for message in request.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert request.system == [
        {"type": "text", "text": "Base instructions."},
        {"type": "text", "text": "Injected reminder."},
        {"type": "text", "text": "Latest reminder."},
    ]

    converted = anthropic_to_openai_request(request)
    assert converted.messages[0].role.value == "system"
    assert converted.messages[0].content == (
        "Base instructions.\nInjected reminder.\nLatest reminder."
    )


def test_anthropic_request_still_rejects_unknown_message_roles() -> None:
    with pytest.raises(ValidationError):
        AnthropicMessageRequest.model_validate({
            "model": "glm-5.2",
            "max_tokens": 256,
            "messages": [{"role": "tool", "content": "not valid here"}],
        })


def test_openai_text_response_converts_to_anthropic_message() -> None:
    response = openai_to_anthropic_response(
        {
            "id": "chatcmpl-test",
            "choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        },
        "claude-test",
    )

    assert response["id"] == "chatcmpl-test"
    assert response["content"] == [{"type": "text", "text": "hello"}]
    assert response["stop_reason"] == "end_turn"
    assert response["usage"] == {"input_tokens": 8, "output_tokens": 4}


def test_openai_tool_calls_convert_to_anthropic_tool_use_blocks() -> None:
    response = openai_to_anthropic_response(
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": '{"command":"ls"}',
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": {"path": "README.md"},
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
        },
        "claude-test",
    )

    assert response["stop_reason"] == "tool_use"
    assert response["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "Bash",
            "input": {"command": "ls"},
        },
        {
            "type": "tool_use",
            "id": "call_2",
            "name": "Read",
            "input": {"path": "README.md"},
        },
    ]


def test_empty_openai_response_converts_to_empty_anthropic_text_block() -> None:
    response = openai_to_anthropic_response(
        {"choices": [{"message": {}, "finish_reason": "stop"}]},
        "claude-test",
    )

    assert response["content"] == [{"type": "text", "text": ""}]


def test_anthropic_usage_updates_quota_and_creates_request_log() -> None:
    token = SimpleNamespace(
        id=3,
        request_count=0,
        token_count=0,
        used_quota=90,
        quota=100,
        enabled=True,
        last_used_at=None,
        user_id="user-1",
    )
    channel = SimpleNamespace(
        id=7,
        type="openai",
        protocol="openai",
    )

    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

    db = FakeDb()
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    async def record():
        await accounting_service.record_success(
            db,
            request_id="req-test",
            conversation_id=None,
            request_protocol="anthropic_messages",
            token=token,
            channel=channel,
            model="claude-test",
            provider_model="provider-model",
            usage=accounting_service.extract_usage({
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                }
            }),
            latency_ms=250,
            client_ip=request.client.host,
        )

    asyncio.run(record())

    assert token.request_count == 1
    assert token.token_count == 12
    assert token.used_quota == 102
    assert token.enabled is False
    assert len(db.added) == 2
    assert db.added[0].total_tokens == 12
    assert db.added[1].request_protocol == "anthropic_messages"
    assert db.added[1].total_tokens == 12


def test_streaming_usage_missing_when_provider_sends_no_usage() -> None:
    usage = accounting_service.streaming_usage(
        prompt_tokens=0,
        completion_tokens=0,
        has_provider_usage=False,
    )

    assert usage.usage_source == "missing"
    assert usage.total_tokens == 0


def test_streaming_usage_provider_when_usage_chunk_is_seen() -> None:
    usage = accounting_service.streaming_usage(
        prompt_tokens=8,
        completion_tokens=4,
        has_provider_usage=True,
    )

    assert usage.usage_source == "provider"
    assert usage.prompt_tokens == 8
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 12


def test_anthropic_request_preserves_tools_and_tool_messages() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "stream": True,
        "stop_sequences": ["STOP"],
        "tools": [{
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        }],
        "tool_choice": {"type": "tool", "name": "Bash"},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "README.md",
                }],
            },
        ],
    })

    converted = anthropic_to_openai_request(request)

    assert converted.stop == ["STOP"]
    assert converted.tools[0].function.name == "Bash"
    assert converted.tool_choice == {
        "type": "function",
        "function": {"name": "Bash"},
    }
    assert converted.messages[0].tool_calls[0].id == "toolu_1"
    assert json.loads(converted.messages[0].tool_calls[0].function.arguments) == {
        "command": "ls"
    }
    assert converted.messages[1].role.value == "tool"
    assert converted.messages[1].tool_call_id == "toolu_1"
    assert converted.messages[1].content == "README.md"


def test_standard_anthropic_stream_events_convert_to_openai() -> None:
    text = ProtocolConverter.anthropic_stream_to_openai(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        },
        "claude-test",
        "chunk-id",
    )
    arguments = ProtocolConverter.anthropic_stream_to_openai(
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"command":"ls"}',
            },
        },
        "claude-test",
        "chunk-id",
    )
    final = ProtocolConverter.anthropic_stream_to_openai(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 12},
        },
        "claude-test",
        "chunk-id",
    )

    assert text["choices"][0]["delta"]["content"] == "hello"
    assert (
        arguments["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        == '{"command":"ls"}'
    )
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"]["completion_tokens"] == 12


def test_anthropic_message_start_preserves_input_usage() -> None:
    chunk = ProtocolConverter.anthropic_stream_to_openai(
        {
            "type": "message_start",
            "message": {
                "usage": {"input_tokens": 42, "output_tokens": 0},
            },
        },
        "claude-test",
        "chunk-id",
    )

    assert chunk["usage"]["prompt_tokens"] == 42


def test_internal_request_round_trips_to_anthropic_provider_format() -> None:
    source = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "tools": [{
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {"type": "object"},
        }],
        "tool_choice": {"type": "tool", "name": "Bash"},
        "messages": [
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "README.md",
                }],
            },
        ],
    })

    round_tripped = ProtocolConverter.openai_to_anthropic(
        anthropic_to_openai_request(source)
    )

    assert round_tripped["tool_choice"] == {"type": "tool", "name": "Bash"}
    assert round_tripped["messages"][0]["content"][0]["input"] == {
        "command": "ls"
    }
    assert round_tripped["messages"][1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "README.md",
    }


def test_openai_tool_stream_emits_complete_anthropic_event_sequence() -> None:
    converter = OpenAIToAnthropicStreamConverter()

    events = []
    events.extend(converter.feed({
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": ""},
                }],
            },
            "finish_reason": None,
        }],
    }))
    events.extend(converter.feed({
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "function": {"arguments": '{"command":"ls"}'},
                }],
            },
            "finish_reason": None,
        }],
    }))
    events.extend(converter.feed({
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }))
    events.extend(converter.finish(
        usage={"prompt_tokens": 20, "completion_tokens": 8},
    ))

    assert [event["type"] for event in events] == [
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0]["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "Bash",
        "input": {},
    }
    assert events[1]["delta"]["partial_json"] == '{"command":"ls"}'
    assert events[3]["delta"]["stop_reason"] == "tool_use"
    assert events[3]["usage"]["output_tokens"] == 8


def test_openai_stream_waits_for_trailing_usage_before_message_stop() -> None:
    converter = OpenAIToAnthropicStreamConverter()

    finish_events = converter.feed({
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
        "usage": None,
    })
    usage_events = converter.feed({
        "choices": [],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    })
    final_events = converter.finish(
        usage={"prompt_tokens": 20, "completion_tokens": 8},
    )

    assert finish_events == []
    assert usage_events == []
    assert final_events[-2]["usage"]["output_tokens"] == 8
    assert final_events[-1] == {"type": "message_stop"}


def test_anthropic_native_fields_survive_anthropic_upstream_conversion() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "system": [{
            "type": "text",
            "text": "cached system",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "failed",
                "is_error": True,
            }],
        }],
    })
    channel = SimpleNamespace(
        model_mapping={"claude-test": "claude-provider"},
        extra={},
    )
    adapter = AnthropicAdapter(channel, None)

    converted = asyncio.run(adapter.convert_request(
        anthropic_to_openai_request(request)
    ))

    assert converted["model"] == "claude-provider"
    assert converted["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert converted["messages"][0]["content"][0]["is_error"] is True


def test_anthropic_request_preserves_claude_code_extension_fields() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "thinking": {"type": "adaptive"},
        "context_management": {"edits": []},
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral"},
        }],
    })
    adapter = AnthropicAdapter(
        SimpleNamespace(model_mapping={}, extra={}), None
    )

    converted = asyncio.run(adapter.convert_request(anthropic_to_openai_request(request)))

    assert converted["thinking"] == {"type": "adaptive"}
    assert converted["context_management"] == {"edits": []}
    assert converted["tools"][0]["cache_control"] == {"type": "ephemeral"}


def test_native_anthropic_response_and_sse_events_are_not_downgraded() -> None:
    class NativeStream(AsyncByteStream):
        async def __aiter__(self):
            events = [
                {"type": "message_start", "message": {"id": "msg_1", "type": "message"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "reason"},
                },
                {"type": "message_stop"},
            ]
            yield "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()

    request = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hello"}],
    })
    internal = anthropic_to_openai_request(request)
    adapter = AnthropicAdapter(SimpleNamespace(model_mapping={}, extra={}), None)
    native = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "reason", "signature": "sig"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    response = Response(
        200, json=native, request=Request("POST", "https://example.com/v1/messages")
    )
    converted = asyncio.run(adapter.convert_response(response, internal))

    stream_response = Response(
        200,
        stream=NativeStream(),
        request=Request("POST", "https://example.com/v1/messages"),
    )

    async def collect():
        return [event async for event in adapter.stream_convert_response(stream_response, internal)]

    events = asyncio.run(collect())

    assert converted == native
    assert events[1]["delta"]["type"] == "thinking_delta"


def test_native_anthropic_count_tokens_and_beta_headers_are_forwarded() -> None:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        return Response(200, json={"input_tokens": 42})

    channel = SimpleNamespace(
        type="anthropic",
        protocol="anthropic",
        base_url="https://example.com/v1",
        key="provider-key",
        model_mapping={"claude-test": "claude-provider"},
        extra={},
    )

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AnthropicAdapter(channel, client)
            await adapter.count_tokens(
                {
                    "model": "claude-test",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                {"anthropic-beta": "token-counting-2024-11-01"},
            )

    asyncio.run(exercise())

    assert seen[0].url.path == "/v1/messages/count_tokens"
    assert seen[0].headers["anthropic-beta"] == "token-counting-2024-11-01"
    assert json.loads(seen[0].content)["model"] == "claude-provider"


def test_provider_adapters_forward_tools() -> None:
    anthropic_request = AnthropicMessageRequest.model_validate({
        "model": "test-model",
        "max_tokens": 128,
        "tools": [{
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {"type": "object"},
        }],
        "tool_choice": {"type": "tool", "name": "Bash"},
        "messages": [{"role": "user", "content": "list files"}],
    })
    request = anthropic_to_openai_request(anthropic_request)
    channel = SimpleNamespace(model_mapping={}, extra={})

    async def convert(adapter_class):
        return await adapter_class(channel, None).convert_request(request)

    for adapter_class in (ZhipuAdapter, MiniMaxAdapter, KimiAdapter):
        body = asyncio.run(convert(adapter_class))
        assert body["tools"][0]["function"]["name"] == "Bash"
        assert body["tool_choice"]["function"]["name"] == "Bash"


def test_openai_streaming_request_explicitly_requests_usage() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "test-model",
        "max_tokens": 128,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    })
    channel = SimpleNamespace(model_mapping={}, extra={})

    internal_request = anthropic_to_openai_request(request)

    for adapter_class in (
        OpenAIAdapter,
        ZhipuAdapter,
        MiniMaxAdapter,
        KimiAdapter,
        MoonshotAdapter,
    ):
        adapter = adapter_class(channel, None)
        body = asyncio.run(adapter.convert_request(internal_request))
        prepared = adapter.prepare_request_body(internal_request, body)
        assert prepared["stream_options"] == {"include_usage": True}


def test_moonshot_adapter_allows_missing_extra_config() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "test-model",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hello"}],
    })
    channel = SimpleNamespace(model_mapping={}, extra=None)

    body = asyncio.run(
        MoonshotAdapter(channel, None).convert_request(
            anthropic_to_openai_request(request)
        )
    )

    assert body["model"] == "test-model"
    assert "enable_search" not in body
