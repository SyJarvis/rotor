import asyncio
import json
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from rotor.adapters.factory import AdapterFactory
from rotor.schemas.request import ChatCompletionRequest


@pytest.mark.parametrize(
    "provider", ["openai", "azure", "deepseek", "kimi", "minimax", "moonshot", "zhipu"]
)
@pytest.mark.parametrize("stream", [False, True])
def test_native_chat_fields_reach_provider_http_request(provider, stream) -> None:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        if stream:
            return Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )
        return Response(200, json={"choices": []})

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }
    messages = [
        {"role": "user", "content": "Earlier question"},
        {
            "role": "assistant",
            "content": "Earlier answer",
            "reasoning_content": "Retained reasoning history",
        },
        {"role": "user", "content": "Answer using the requested format"},
    ]
    request = ChatCompletionRequest.model_validate({
        "model": "public-model",
        "messages": messages,
        "reasoning_effort": "high",
        "response_format": response_format,
        "parallel_tool_calls": False,
        "max_completion_tokens": 0,
        "temperature": 0,
        "top_p": 0,
        "stream": stream,
        "anthropic_payload": {"system": "internal Anthropic payload"},
        "anthropic_headers": {"anthropic-beta": "internal-beta"},
        "responses_payload": {"input": "internal Responses payload"},
        "responses_cacheable_system_content": [{"type": "text", "text": "cache"}],
        "responses_prompt_cache_key": "internal-cache-key",
        "unknown_option": "not forwarded",
    })
    # Zhipu uses the native Anthropic path when an Anthropic payload is present.
    if provider == "zhipu":
        request.anthropic_payload = None
    channel = SimpleNamespace(
        type=provider,
        protocol="openai",
        base_url="https://example.com/v1",
        key="provider-key",
        model_mapping={"public-model": "provider-model"},
        extra={},
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

    assert len(seen) == 1
    assert seen[0].url.path == "/v1/chat/completions"
    assert "anthropic-beta" not in seen[0].headers
    body = json.loads(seen[0].content)
    assert body["model"] == "provider-model"
    assert body["messages"] == messages
    assert body["reasoning_effort"] == "high"
    assert body["response_format"] == response_format
    assert body["parallel_tool_calls"] is False
    assert body["max_completion_tokens"] == 0
    assert body["temperature"] == 0
    assert body["top_p"] == 0
    assert body["stream"] is stream
    assert not any(key.startswith(("anthropic_", "responses_")) for key in body)
    assert "unknown_option" not in body
    if stream and provider != "zhipu":
        assert body["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in body


@pytest.mark.parametrize("optional_fields", [{}, {
    "reasoning_effort": None,
    "response_format": None,
    "parallel_tool_calls": None,
    "max_completion_tokens": None,
}])
def test_absent_native_chat_fields_are_not_added(optional_fields) -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        bodies.append(json.loads(request.content))
        return Response(200, json={"choices": []})

    request = ChatCompletionRequest.model_validate({
        "model": "public-model",
        "messages": [{"role": "user", "content": "hello"}],
        **optional_fields,
    })
    channel = SimpleNamespace(
        type="openai",
        protocol="openai",
        base_url="https://example.com/v1",
        key="provider-key",
        model_mapping={},
        extra={},
    )

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            await AdapterFactory.create_adapter(channel, client).make_request(request)

    asyncio.run(exercise())

    assert len(bodies) == 1
    for key in (
        "reasoning_effort", "response_format", "parallel_tool_calls", "max_completion_tokens"
    ):
        assert key not in bodies[0]
    assert "reasoning_content" not in bodies[0]["messages"][0]


@pytest.mark.parametrize("stream", [False, True])
def test_tool_choice_none_withholds_anthropic_tools_and_preserves_history(stream) -> None:
    bodies: list[dict] = []

    async def handler(request: Request) -> Response:
        assert request.url.path == "/v1/messages"
        bodies.append(json.loads(request.content))
        return Response(200, json={"content": []})

    request = ChatCompletionRequest.model_validate({
        "model": "public-model",
        "stream": stream,
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        "tool_choice": "none",
        "messages": [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
            {"role": "user", "content": "Summarize without calling tools"},
        ],
    })
    channel = SimpleNamespace(
        type="anthropic",
        protocol="anthropic",
        base_url="https://example.com/v1",
        key="provider-key",
        model_mapping={"public-model": "provider-model"},
        extra={},
    )

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            response = await AdapterFactory.create_adapter(channel, client).make_request(request)
            try:
                await response.aread()
            finally:
                await response.aclose()

    asyncio.run(exercise())

    assert len(bodies) == 1
    body = bodies[0]
    assert body["model"] == "provider-model"
    assert body["stream"] is stream
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {}}],
    }
    assert body["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "Sunny"},
            {"type": "text", "text": "Summarize without calling tools"},
        ],
    }
    assert request.tools[0].function.name == "get_weather"
    assert request.tool_choice == "none"
