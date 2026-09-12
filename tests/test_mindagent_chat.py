import asyncio
import sys
import types

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import StreamingResponse

import rotor.api.admin.mindagent as endpoint
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.adapters.protocol.responses import chat_request_to_responses_payload
from mindagent.context import ContextConfig, ContextManager
from mindagent.core import AgentContext
from rotor.schemas.request import ChatCompletionRequest


def test_chat_request_requires_latest_user_message():
    with pytest.raises(ValidationError):
        endpoint.MindAgentChatRequest(
            conversation_id="chat-test",
            model="fake-model",
            messages=[{"role": "assistant", "content": "not a user turn"}],
        )


def test_decode_gateway_events_ignores_done_marker():
    events = endpoint._decode_gateway_events(
        'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    assert events[0]["choices"][0]["delta"]["content"] == "hello"
    assert len(events) == 1


def test_mcp_registry_does_not_require_mcp_tool_set_when_unconfigured(
    monkeypatch,
):
    class FakeToolRegistry:
        pass

    tools = types.ModuleType("mindagent.tools")
    tools.ToolRegistry = FakeToolRegistry
    monkeypatch.setitem(sys.modules, "mindagent.tools", tools)
    monkeypatch.setattr(endpoint.settings, "ROTOR_MINDAGENT_MCP_COMMAND", None)

    registry = asyncio.run(endpoint._build_rotor_mcp_registry("run-1"))

    assert isinstance(registry, FakeToolRegistry)


def test_image_content_converts_for_anthropic_and_responses():
    request = ChatCompletionRequest.model_validate({
        "model": "vision-model",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,dGVzdA==",
                    },
                },
            ],
        }],
    })

    anthropic = ProtocolConverter.openai_to_anthropic(request)
    assert anthropic["messages"][0]["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "dGVzdA==",
    }

    responses = chat_request_to_responses_payload(request)
    assert responses["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,dGVzdA==",
    }


def test_chat_request_moves_system_messages_to_stable_leading_prefix():
    request = ChatCompletionRequest.model_validate({
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "first user message"},
            {"role": "system", "content": "first instruction"},
            {"role": "assistant", "content": "first response"},
            {"role": "system", "content": "second instruction"},
            {"role": "user", "content": "second user message"},
        ],
    })

    assert [message.role.value for message in request.messages] == [
        "system", "system", "user", "assistant", "user",
    ]
    assert [message.content for message in request.messages] == [
        "first instruction", "second instruction", "first user message",
        "first response", "second user message",
    ]


def test_context_manager_keeps_default_system_before_history():
    context = AgentContext(
        user_input="new user message",
        run_id="context-order",
        messages=[
            {"role": "user", "content": "earlier user message"},
            {"role": "assistant", "content": "earlier response"},
        ],
    )
    manager = ContextManager(ContextConfig(system_prompt="MindAgent instructions"))

    asyncio.run(manager.build_context(context))

    assert context.messages == [
        {"role": "system", "content": "MindAgent instructions"},
        {"role": "user", "content": "earlier user message"},
        {"role": "assistant", "content": "earlier response"},
        {"role": "user", "content": "new user message"},
    ]


def test_gateway_provider_forwards_tools_and_decodes_tool_calls(
    monkeypatch,
):
    captured = {}

    async def fake_chat_completions(
        request_data,
        http_request,
        response,
        db,
        token,
    ):
        captured["request"] = request_data

        async def stream():
            yield (
                'data: {"choices":[{"delta":{"tool_calls":[{'
                '"index":0,"id":"call-1","function":{'
                '"name":"rotor_get_request_trace",'
                '"arguments":"{\\"request_id\\":\\"req-123\\"}"}}]},'
                '"finish_reason":"tool_calls"}]}\n\n'
            )

        return StreamingResponse(stream())

    monkeypatch.setattr(
        endpoint,
        "chat_completions",
        fake_chat_completions,
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })
    provider = endpoint._build_gateway_provider(
        http_request=request,
        token=object(),
        model="fake-model",
    )
    tool_schema = {
        "type": "function",
        "function": {
            "name": "rotor_get_request_trace",
            "description": "Get trace",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                },
                "required": ["request_id"],
            },
        },
    }

    async def collect():
        return [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "diagnose"}],
                tools=[tool_schema],
            )
        ]

    chunks = asyncio.run(collect())

    assert captured["request"].tools[0].function.name == (
        "rotor_get_request_trace"
    )
    assert captured["request"].tool_choice == "auto"
    assert chunks[0].tool_call_deltas[0].name == (
        "rotor_get_request_trace"
    )
    assert chunks[0].tool_call_deltas[0].arguments_delta == (
        '{"request_id":"req-123"}'
    )


def test_mindagent_runtime_streams_deltas(monkeypatch):
    mindagent = pytest.importorskip("mindagent.providers")
    seen_messages = []

    class FakeProvider(mindagent.BaseProvider):
        name = "fake"
        capabilities = mindagent.ProviderCapabilities(
            text=True,
            vision=True,
            streaming=True,
            max_context_tokens=128_000,
        )

        async def chat(self, messages, **kwargs):
            return mindagent.ProviderResponse(content="你好")

        async def stream_chat(self, messages, **kwargs):
            seen_messages.extend(messages)
            for part in ("你", "好"):
                yield mindagent.ProviderStreamChunk(
                    content_delta=part,
                    model="fake-model",
                )

    monkeypatch.setattr(
        endpoint,
        "_build_gateway_provider",
        lambda **kwargs: FakeProvider(),
    )
    request_data = endpoint.MindAgentChatRequest(
        conversation_id="chat-test",
        model="fake-model",
        messages=[{"role": "user", "content": "你好"}],
        attachments=[
            {
                "kind": "image",
                "name": "sample.png",
                "mime_type": "image/png",
                "size": 4,
                "data_url": "data:image/png;base64,dGVzdA==",
            },
            {
                "kind": "file",
                "name": "notes.txt",
                "mime_type": "text/plain",
                "size": 5,
                "content": "hello",
            },
        ],
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })

    async def collect():
        return "".join([
            chunk
            async for chunk in endpoint._run_mindagent(
                request_data,
                request,
                object(),
            )
        ])

    output = asyncio.run(collect())
    assert '"type": "delta"' in output
    assert '"delta": "你"' in output
    assert '"type": "done"' in output
    assert '"answer": "你好"' in output
    assert output.index('"type": "delta"') < output.index('"type": "done"')
    user_content = next(
        message["content"]
        for message in seen_messages
        if message["role"] == "user" and isinstance(message["content"], list)
    )
    assert user_content[1]["image_url"]["url"].startswith("data:image/png")
    assert "notes.txt" in user_content[0]["text"]


def test_mindagent_web_chat_disables_short_reasoning_step_timeout(monkeypatch):
    mindagent = pytest.importorskip("mindagent.providers")
    import mindagent.core as mindagent_core

    captured = {}
    original_run_config = mindagent_core.RunConfig

    def recording_run_config(**kwargs):
        captured.update(kwargs)
        return original_run_config(**kwargs)

    class FakeProvider(mindagent.BaseProvider):
        name = "fake"
        capabilities = mindagent.ProviderCapabilities(
            text=True,
            streaming=True,
            max_context_tokens=128_000,
        )

        async def chat(self, messages, **kwargs):
            return mindagent.ProviderResponse(content="ok")

        async def stream_chat(self, messages, **kwargs):
            yield mindagent.ProviderStreamChunk(
                content_delta="ok",
                finish_reason="stop",
            )

    monkeypatch.setattr(mindagent_core, "RunConfig", recording_run_config)
    monkeypatch.setattr(
        endpoint,
        "_build_gateway_provider",
        lambda **kwargs: FakeProvider(),
    )
    request_data = endpoint.MindAgentChatRequest(
        conversation_id="chat-timeout",
        model="fake-model",
        messages=[{"role": "user", "content": "hello"}],
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })

    async def collect():
        return "".join([
            chunk
            async for chunk in endpoint._run_mindagent(
                request_data,
                request,
                object(),
            )
        ])

    asyncio.run(collect())

    assert captured["step_timeout_s"] is None
    assert captured["total_timeout_s"] == 300


def test_mindagent_runtime_executes_discovered_mcp_tool(monkeypatch):
    mindagent = pytest.importorskip("mindagent.providers")
    from mindagent.core import ActionRisk
    from mindagent.tools import BaseTool, ToolDefinition, ToolRegistry

    calls = []
    provider_requests = []

    class FakeTraceTool(BaseTool):
        definition = ToolDefinition(
            name="rotor_get_request_trace",
            description="Get a Rotor request trace",
            parameters={
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
            risk=ActionRisk.READ_ONLY,
        )

        async def execute(self, arguments, context):
            calls.append((arguments, context.run_id))
            return {
                "data": {
                    "request_id": arguments["request_id"],
                    "attempts": [
                        {
                            "outcome": "error",
                            "error": {
                                "category": "upstream_availability",
                            },
                        }
                    ],
                }
            }

    class ToolCallingProvider(mindagent.BaseProvider):
        name = "fake"
        capabilities = mindagent.ProviderCapabilities(
            text=True,
            tool_calling=True,
            streaming=True,
            max_context_tokens=128_000,
        )

        async def chat(self, messages, **kwargs):
            raise AssertionError("stream_chat should be used")

        async def stream_chat(self, messages, **kwargs):
            provider_requests.append((messages, kwargs))
            if len(provider_requests) == 1:
                yield mindagent.ProviderStreamChunk(
                    tool_call_deltas=[
                        mindagent.ProviderToolCallDelta(
                            index=0,
                            id="call-1",
                            name="rotor_get_request_trace",
                            arguments_delta='{"request_id":"req-123"}',
                        )
                    ],
                    finish_reason="tool_calls",
                )
                return
            yield mindagent.ProviderStreamChunk(
                content_delta="上游渠道不可用",
                finish_reason="stop",
            )

    async def build_registry(_run_id):
        return ToolRegistry([FakeTraceTool()])

    monkeypatch.setattr(
        endpoint,
        "_build_rotor_mcp_registry",
        build_registry,
    )
    monkeypatch.setattr(
        endpoint,
        "_build_gateway_provider",
        lambda **kwargs: ToolCallingProvider(),
    )
    request_data = endpoint.MindAgentChatRequest(
        conversation_id="chat-test",
        model="fake-model",
        messages=[
            {
                "role": "user",
                "content": "诊断请求 req-123",
            }
        ],
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })

    async def collect():
        return "".join([
            chunk
            async for chunk in endpoint._run_mindagent(
                request_data,
                request,
                object(),
            )
        ])

    output = asyncio.run(collect())

    assert calls[0][0] == {"request_id": "req-123"}
    assert calls[0][1].startswith("chat-")
    assert provider_requests[0][1]["tools"][0]["function"]["name"] == (
        "rotor_get_request_trace"
    )
    assert "upstream_availability" in str(provider_requests[1][0])
    assert '"answer": "上游渠道不可用"' in output


def test_gateway_provider_reports_upstream_failure_detail(monkeypatch):
    import httpx

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc_info):
            return False

    async def failing_chat_completions(*args, **kwargs):
        request = httpx.Request(
            "POST", "https://opencode.ai/zen/go/v1/chat/completions"
        )
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "unknown model"}},
        )
        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request'",
            request=request,
            response=response,
        )

    monkeypatch.setattr(endpoint, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(endpoint, "chat_completions", failing_chat_completions)

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })
    provider = endpoint._build_gateway_provider(
        http_request=request,
        token=object(),
        model="fake-model",
    )

    async def consume():
        async for _ in provider.stream_chat(
            [{"role": "user", "content": "hi"}]
        ):
            pass

    with pytest.raises(RuntimeError) as error:
        asyncio.run(consume())

    message = str(error.value)
    assert message.startswith("Upstream returned HTTP 400")
    assert "unknown model" in message
    assert "Client error" not in message


def test_gateway_provider_reports_channel_failure_detail(monkeypatch):
    from rotor.core.exceptions import ChannelException

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc_info):
            return False

    async def failing_chat_completions(*args, **kwargs):
        raise ChannelException(
            "All channels failed for model 'fake-model'",
            status_code=502,
            original_error="ConnectError: connection refused",
        )

    monkeypatch.setattr(endpoint, "async_session_maker", lambda: FakeSession())
    monkeypatch.setattr(endpoint, "chat_completions", failing_chat_completions)

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })
    provider = endpoint._build_gateway_provider(
        http_request=request,
        token=object(),
        model="fake-model",
    )

    async def consume():
        async for _ in provider.stream_chat(
            [{"role": "user", "content": "hi"}]
        ):
            pass

    with pytest.raises(RuntimeError) as error:
        asyncio.run(consume())

    assert str(error.value) == (
        "All channels failed for model 'fake-model': "
        "ConnectError: connection refused"
    )


def test_runtime_streams_provider_failure_message_to_the_chat(monkeypatch):
    mindagent = pytest.importorskip("mindagent.providers")

    class FailingProvider(mindagent.BaseProvider):
        name = "fake"
        capabilities = mindagent.ProviderCapabilities(
            text=True,
            streaming=True,
            max_context_tokens=128_000,
        )

        async def chat(self, messages, **kwargs):
            raise RuntimeError("Upstream returned HTTP 400: unknown model")

        async def stream_chat(self, messages, **kwargs):
            raise RuntimeError("Upstream returned HTTP 400: unknown model")
            yield mindagent.ProviderStreamChunk(content_delta="")

    monkeypatch.setattr(
        endpoint,
        "_build_gateway_provider",
        lambda **kwargs: FailingProvider(),
    )
    request_data = endpoint.MindAgentChatRequest(
        conversation_id="chat-failure",
        model="fake-model",
        messages=[{"role": "user", "content": "你好"}],
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1),
    })

    async def collect():
        return "".join([
            chunk
            async for chunk in endpoint._run_mindagent(
                request_data,
                request,
                object(),
            )
        ])

    output = asyncio.run(collect())

    assert '"type": "error"' in output
    assert "Upstream returned HTTP 400: unknown model" in output
