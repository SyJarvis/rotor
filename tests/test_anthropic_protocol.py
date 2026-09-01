import json
import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncByteStream, AsyncClient, MockTransport, Request, Response
from pydantic import ValidationError
from starlette.requests import Request as StarletteRequest

import rotor.api.v1.anthropic as anthropic_endpoint
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.adapters.protocol.anthropic import AnthropicAdapter
from rotor.adapters.protocol.responses import OpenAIResponsesAdapter
from rotor.core.exceptions import UpstreamOverloaded
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


def test_dynamic_billing_header_does_not_change_cross_protocol_system() -> None:
    def convert(cch: str):
        request = AnthropicMessageRequest.model_validate({
            "model": "glm-5.2",
            "max_tokens": 256,
            "system": [
                {
                    "type": "text",
                    "text": (
                        "x-anthropic-billing-header: "
                        "cc_version=2.1.98.3ea; cc_entrypoint=cli; "
                        f"cch={cch};"
                    ),
                },
                {
                    "type": "text",
                    "text": "Stable system instructions.",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": "hello"}],
        })
        return anthropic_to_openai_request(request)

    first = convert("11111")
    second = convert("22222")

    assert first.messages[0].content == "Stable system instructions."
    assert first.messages == second.messages
    assert first.anthropic_payload["system"][0]["text"].endswith("cch=11111;")
    assert second.anthropic_payload["system"][0]["text"].endswith("cch=22222;")


def test_anthropic_cache_control_maps_to_gpt56_responses_breakpoints() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "gpt-5.6-sol",
        "max_tokens": 256,
        "system": [
            {
                "type": "text",
                "text": (
                    "x-anthropic-billing-header: "
                    "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=abc12;"
                ),
            },
            {"type": "text", "text": "Stable A."},
            {
                "type": "text",
                "text": "Stable B.",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [{"role": "user", "content": "hello"}],
    })
    internal = anthropic_to_openai_request(request)
    internal.responses_prompt_cache_key = "rotor_cache_key"
    adapter = OpenAIResponsesAdapter(
        SimpleNamespace(model_mapping={}, extra={}),
        None,
    )

    body = asyncio.run(adapter.convert_request(internal))

    assert body["prompt_cache_key"] == "rotor_cache_key"
    assert body["prompt_cache_options"] == {
        "mode": "implicit",
        "ttl": "30m",
    }
    assert body["input"][0] == {
        "type": "message",
        "role": "developer",
        "content": [
            {"type": "input_text", "text": "Stable A.\n"},
            {
                "type": "input_text",
                "text": "Stable B.",
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
        ],
    }


def test_anthropic_cache_control_does_not_add_gpt56_fields_to_older_model() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "gpt-5.5",
        "max_tokens": 256,
        "system": [{
            "type": "text",
            "text": "Stable system.",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": "hello"}],
    })
    internal = anthropic_to_openai_request(request)
    internal.responses_prompt_cache_key = "rotor_cache_key"
    adapter = OpenAIResponsesAdapter(
        SimpleNamespace(model_mapping={}, extra={}),
        None,
    )

    body = asyncio.run(adapter.convert_request(internal))

    assert "prompt_cache_key" not in body
    assert "prompt_cache_options" not in body
    assert "prompt_cache_breakpoint" not in json.dumps(body)


def test_anthropic_cache_control_limits_gpt56_breakpoints_to_last_four() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "gpt-5.6-sol",
        "max_tokens": 256,
        "system": [
            {
                "type": "text",
                "text": f"Stable {index}.",
                "cache_control": {"type": "ephemeral"},
            }
            for index in range(5)
        ],
        "messages": [{"role": "user", "content": "hello"}],
    })
    internal = anthropic_to_openai_request(request)
    adapter = OpenAIResponsesAdapter(
        SimpleNamespace(model_mapping={}, extra={}),
        None,
    )

    body = asyncio.run(adapter.convert_request(internal))
    content = body["input"][0]["content"]

    assert "prompt_cache_breakpoint" not in content[0]
    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        for block in content[1:]
    )


def test_dynamic_billing_header_line_is_removed_from_string_system() -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "glm-5.2",
        "max_tokens": 256,
        "system": (
            "x-anthropic-billing-header: "
            "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=abc12;\n"
            "Stable system instructions."
        ),
        "messages": [{"role": "user", "content": "hello"}],
    })

    converted = anthropic_to_openai_request(request)

    assert converted.messages[0].content == "Stable system instructions."


def test_nonleading_billing_header_system_block_is_preserved() -> None:
    billing_header = (
        "x-anthropic-billing-header: "
        "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=abc12;"
    )
    request = AnthropicMessageRequest.model_validate({
        "model": "glm-5.2",
        "max_tokens": 256,
        "system": [
            {"type": "text", "text": "Meaningful first block."},
            {"type": "text", "text": billing_header},
        ],
        "messages": [{"role": "user", "content": "hello"}],
    })

    converted = anthropic_to_openai_request(request)

    assert converted.messages[0].content == (
        f"Meaningful first block.\n{billing_header}"
    )


@pytest.mark.parametrize(
    "first_block",
    [
        {
            "type": "text",
            "text": (
                "x-anthropic-billing-header: "
                "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=not-hex;"
            ),
        },
        {
            "type": "text",
            "text": (
                "x-anthropic-billing-header: "
                "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=abc12;"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ],
)
def test_ambiguous_billing_header_system_block_is_preserved(first_block) -> None:
    request = AnthropicMessageRequest.model_validate({
        "model": "glm-5.2",
        "max_tokens": 256,
        "system": [
            first_block,
            {"type": "text", "text": "Stable system instructions."},
        ],
        "messages": [{"role": "user", "content": "hello"}],
    })

    converted = anthropic_to_openai_request(request)

    assert converted.messages[0].content.startswith(
        "x-anthropic-billing-header:"
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
    assert response["usage"] == {
        "input_tokens": 8,
        "output_tokens": 4,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_openai_cache_usage_converts_to_disjoint_anthropic_fields() -> None:
    response = openai_to_anthropic_response(
        {
            "choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_tokens_details": {
                    "cached_tokens": 60,
                    "cache_write_tokens": 30,
                    "cache_write_5m_tokens": 20,
                    "cache_write_1h_tokens": 10,
                },
            },
        },
        "claude-test",
    )

    assert response["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 60,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 20,
            "ephemeral_1h_input_tokens": 10,
        },
    }


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


def test_anthropic_to_openai_usage_preserves_cache_breakdown() -> None:
    from rotor.schemas.request import AnthropicUsage

    usage = AnthropicUsage(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=300,
        cache_creation={
            "ephemeral_5m_input_tokens": 120,
            "ephemeral_1h_input_tokens": 80,
        },
    )
    result = ProtocolConverter.anthropic_to_openai_usage(usage)

    assert result.prompt_tokens == 600
    assert result.completion_tokens == 50
    assert result.total_tokens == 650
    assert result.prompt_tokens_details == {
        "cached_tokens": 300,
        "cache_write_tokens": 200,
        "cache_write_5m_tokens": 120,
        "cache_write_1h_tokens": 80,
        "uncached_tokens": 100,
    }


def test_anthropic_stream_message_start_preserves_cache_tokens() -> None:
    """message_start stream event carries cache_read_input_tokens into OpenAI chunk."""
    event = {
        "type": "message_start",
        "message": {
            "id": "msg_test",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 0,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 300,
            },
        },
    }
    chunk = ProtocolConverter.anthropic_stream_to_openai(event, "claude-test", "chatcmpl-test")

    assert chunk is not None
    usage = chunk["usage"]
    assert usage["prompt_tokens"] == 600
    assert usage["prompt_tokens_details"]["cached_tokens"] == 300
    assert usage["prompt_tokens_details"]["cache_write_tokens"] == 200


def test_anthropic_stream_message_delta_preserves_cache_tokens() -> None:
    """message_delta stream event carries cache_read_input_tokens into OpenAI chunk."""
    event = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 300,
        },
    }
    chunk = ProtocolConverter.anthropic_stream_to_openai(event, "claude-test", "chatcmpl-test")

    assert chunk is not None
    usage = chunk["usage"]
    assert usage["prompt_tokens_details"]["cached_tokens"] == 300


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
    assert converted.messages[0].role.value == "user"
    assert converted.messages[1].tool_calls[0].id == "toolu_1"
    assert json.loads(converted.messages[1].tool_calls[0].function.arguments) == {
        "command": "ls"
    }
    assert converted.messages[2].role.value == "tool"
    assert converted.messages[2].tool_call_id == "toolu_1"
    assert converted.messages[2].content == "README.md"


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
    assert round_tripped["messages"][0]["role"] == "user"
    assert round_tripped["messages"][1]["content"][0]["input"] == {
        "command": "ls"
    }
    assert round_tripped["messages"][2]["content"][0] == {
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


def test_anthropic_native_upstream_preserves_billing_header() -> None:
    billing_header = (
        "x-anthropic-billing-header: "
        "cc_version=2.1.98.3ea; cc_entrypoint=cli; cch=abc12;"
    )
    request = AnthropicMessageRequest.model_validate({
        "model": "claude-test",
        "max_tokens": 256,
        "system": [
            {"type": "text", "text": billing_header},
            {
                "type": "text",
                "text": "cached system",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [{"role": "user", "content": "hello"}],
    })
    adapter = AnthropicAdapter(
        SimpleNamespace(model_mapping={}, extra={}), None
    )

    converted = asyncio.run(adapter.convert_request(
        anthropic_to_openai_request(request)
    ))

    assert converted["system"][0]["text"] == billing_header
    assert converted["system"][1]["cache_control"] == {"type": "ephemeral"}


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


def test_anthropic_stream_overload_error_raises_upstream_overloaded() -> None:
    class OverloadedStream(AsyncByteStream):
        async def __aiter__(self):
            events = [
                {"type": "message_start", "message": {"id": "msg_1", "type": "message"}},
                {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Our servers are currently overloaded. Please try again later.",
                    },
                },
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
    response = Response(
        200,
        stream=OverloadedStream(),
        request=Request("POST", "https://example.com/v1/messages"),
    )

    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(response, internal)
        ]

    with pytest.raises(UpstreamOverloaded) as captured:
        asyncio.run(collect())

    assert captured.value.error_type == "overloaded_error"


def test_anthropic_stream_overload_by_wording_raises_upstream_overloaded() -> None:
    class OverloadedStream(AsyncByteStream):
        async def __aiter__(self):
            events = [{
                "type": "error",
                "error": {"type": "api_error", "message": "Upstream is overloaded now"},
            }]
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
    response = Response(
        200,
        stream=OverloadedStream(),
        request=Request("POST", "https://example.com/v1/messages"),
    )

    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(response, internal)
        ]

    with pytest.raises(UpstreamOverloaded):
        asyncio.run(collect())


def test_anthropic_stream_parameter_error_still_raises_runtime_error() -> None:
    class InvalidRequestStream(AsyncByteStream):
        async def __aiter__(self):
            events = [{
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "max_tokens is too large",
                },
            }]
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
    response = Response(
        200,
        stream=InvalidRequestStream(),
        request=Request("POST", "https://example.com/v1/messages"),
    )

    async def collect():
        return [
            event
            async for event in adapter.stream_convert_response(response, internal)
        ]

    with pytest.raises(RuntimeError) as captured:
        asyncio.run(collect())

    assert not isinstance(captured.value, UpstreamOverloaded)


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


class _TerminalStreamAccounting:
    def __init__(self) -> None:
        self.attempts = []
        self.successes = []

    async def record(self, **kwargs) -> bool:
        self.attempts.append(kwargs)
        kwargs["context"].recorded = True
        return True

    async def record_failure(self, db, **kwargs) -> None:
        return None

    async def record_success(self, db, **kwargs) -> None:
        self.successes.append(kwargs)


class _TerminalConversationStore:
    def __init__(self) -> None:
        self.finishes = []

    async def append_error(self, handle, code, message) -> None:
        return None

    async def finish(self, handle, status, latency_ms) -> None:
        self.finishes.append(status)


class _TerminalStreamDatabase:
    async def close(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _TerminalStreamClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("error_type", "expected_outcome", "expected_status"),
    [
        (RuntimeError, "failed", "failed"),
        (asyncio.CancelledError, "cancelled", "cancelled"),
    ],
)
def test_anthropic_stream_records_terminal_attempt_without_lease(
    monkeypatch,
    error_type,
    expected_outcome,
    expected_status,
) -> None:
    class FailingAdapter:
        async def stream_convert_response(self, response, request):
            if False:
                yield {}
            raise error_type("stream stopped")

        def map_model_name(self, model):
            return f"provider-{model}"

    accounting = _TerminalStreamAccounting()
    conversation_store = _TerminalConversationStore()
    http_client = _TerminalStreamClient()
    monkeypatch.setattr(anthropic_endpoint, "accounting_service", accounting)
    monkeypatch.setattr(anthropic_endpoint, "attempt_recorder", accounting)
    monkeypatch.setattr(
        anthropic_endpoint,
        "conversation_store",
        conversation_store,
    )
    monkeypatch.setattr(
        anthropic_endpoint,
        "async_session_maker",
        _TerminalStreamDatabase,
    )

    request = AnthropicMessageRequest.model_validate({
        "model": "model-a",
        "max_tokens": 128,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    })
    http_request = StarletteRequest({
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })

    async def exercise() -> type[BaseException] | None:
        response = await anthropic_endpoint._handle_streaming_request(
            request,
            anthropic_to_openai_request(request),
            FailingAdapter(),
            SimpleNamespace(id=2, protocol="openai"),
            SimpleNamespace(id=1),
            _TerminalStreamDatabase(),
            0.0,
            http_request,
            http_client,
            "req-terminal",
            "conv-1",
            object(),
            SimpleNamespace(conversation_id="conv-1"),
            attempt_context=anthropic_endpoint.AttemptContext.start(0),
            lease_session_id="session-a",
        )
        try:
            [chunk async for chunk in response.body_iterator]
        except BaseException as exc:
            return type(exc)
        return None

    raised = asyncio.run(exercise())

    assert accounting.attempts[0]["outcome"] == expected_outcome
    assert accounting.successes == []
    assert conversation_store.finishes[-1] == expected_status
    assert http_client.closed is True
    assert raised is (asyncio.CancelledError if expected_outcome == "cancelled" else None)
