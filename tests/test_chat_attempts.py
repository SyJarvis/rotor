import unittest
from types import SimpleNamespace

import httpx
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.chat as chat_endpoint
from rotor.gateway.accounting import UsageData
from rotor.gateway.attempts import AttemptContext
from rotor.schemas.request import ChatCompletionRequest


class FakeDatabase:
    def __init__(self) -> None:
        self.commits = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        self.closed = True


class FakeConversationStore:
    def __init__(self) -> None:
        self.errors = []

    async def start(self, db, **kwargs):
        return SimpleNamespace(conversation_id=kwargs["conversation_id"])

    async def append_routing(self, handle, channel) -> None:
        return None

    async def append_error(self, handle, code, message) -> None:
        self.errors.append((code, message))

    async def append_response(self, handle, response) -> None:
        return None

    async def append_usage(self, handle, usage) -> None:
        return None

    async def finish(self, handle, status, latency_ms) -> None:
        return None


class RecordingAccounting:
    def __init__(self) -> None:
        self.attempts = []

    def record_routing_decision(self, db, **kwargs) -> None:
        return None

    async def record(self, **kwargs) -> bool:
        self.attempts.append(kwargs)
        kwargs["context"].recorded = True
        return True

    async def record_failure(self, db, **kwargs) -> None:
        return None

    async def record_success(self, db, **kwargs) -> None:
        return None

    def extract_usage(self, response_data) -> UsageData:
        return UsageData()

    def streaming_usage(self, **kwargs) -> UsageData:
        return UsageData()


class FailingSuccessAccounting(RecordingAccounting):
    async def record_success(self, db, **kwargs) -> None:
        raise RuntimeError("usage accounting failed")


class FakeRoutingEngine:
    def __init__(self, channels) -> None:
        self.channels = channels
        self.unavailable = []

    def route(self, channels, **kwargs):
        return SimpleNamespace(
            candidates=self.channels,
            strategy="fallback_order",
            scores=None,
        )

    def begin_attempt(self, model, channel) -> None:
        return None

    def mark_unavailable(self, model, channel, cooldown_seconds=None) -> None:
        self.unavailable.append(channel.id)


class FailingAdapter:
    async def make_request(self, request):
        upstream_request = httpx.Request(
            "POST",
            "https://provider.example/v1/chat",
        )
        response = httpx.Response(
            503,
            request=upstream_request,
            json={"error": {"message": "unavailable"}},
        )
        raise httpx.HTTPStatusError(
            "unavailable",
            request=upstream_request,
            response=response,
        )


class SuccessfulAdapter:
    async def make_request(self, request):
        return object()

    async def convert_response(self, response, request):
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }

    def map_model_name(self, model):
        return f"provider-{model}"


class FailingConversionAdapter:
    async def make_request(self, request):
        return object()

    async def convert_response(self, response, request):
        raise RuntimeError("response conversion failed")


class SuccessfulStreamingAdapter:
    async def stream_convert_response(self, response, request):
        yield {
            "choices": [
                {
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        }

    def map_model_name(self, model):
        return f"provider-{model}"


class FailingStreamingAdapter:
    async def stream_convert_response(self, response, request):
        upstream_request = httpx.Request(
            "POST",
            "https://provider.example/v1/chat",
        )
        if False:
            yield {}
        raise httpx.ReadTimeout("timed out", request=upstream_request)

    def map_model_name(self, model):
        return f"provider-{model}"


class FakeAdapterFactory:
    adapters = {}

    @classmethod
    def create_adapter(cls, channel, http_client):
        return cls.adapters[channel.id]


class FakeStreamSession:
    def __init__(self, token) -> None:
        self.token = token
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def get(self, model, identity):
        return self.token

    async def commit(self) -> None:
        self.commits += 1


class FakeSessionMaker:
    def __init__(self, token) -> None:
        self.token = token
        self.sessions = []

    def __call__(self):
        session = FakeStreamSession(self.token)
        self.sessions.append(session)
        return session


class FakeHttpClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class ChatAttemptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.originals = {
            "accounting_service": chat_endpoint.accounting_service,
            "attempt_recorder": chat_endpoint.attempt_recorder,
            "conversation_store": chat_endpoint.conversation_store,
            "routing_engine": chat_endpoint.routing_engine,
            "AdapterFactory": chat_endpoint.AdapterFactory,
            "get_available_channels": chat_endpoint.get_available_channels,
            "async_session_maker": chat_endpoint.async_session_maker,
        }

    async def asyncTearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(chat_endpoint, name, value)

    async def test_fallback_records_failed_then_successful_attempt(self) -> None:
        channels = [
            SimpleNamespace(
                id=1,
                name="first",
                protocol="openai",
                model_mapping={},
            ),
            SimpleNamespace(
                id=2,
                name="second",
                protocol="openai",
                model_mapping={"model-a": "provider-model-a"},
            ),
        ]
        accounting = RecordingAccounting()
        routing = FakeRoutingEngine(channels)
        FakeAdapterFactory.adapters = {
            1: FailingAdapter(),
            2: SuccessfulAdapter(),
        }

        async def available_channels(model, token, db):
            return channels

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        chat_endpoint.routing_engine = routing
        chat_endpoint.AdapterFactory = FakeAdapterFactory
        chat_endpoint.get_available_channels = available_channels

        request = ChatCompletionRequest.model_validate(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"x-request-id", b"req-1")],
                "client": ("127.0.0.1", 1),
            }
        )
        http_request.state.request_origin = "rotor_agent"
        http_request.state.agent_run_id = "run-1"
        db = FakeDatabase()

        result = await chat_endpoint.chat_completions(
            request,
            http_request,
            Response(),
            db,
            SimpleNamespace(id=1),
        )

        assert result["choices"][0]["message"]["content"] == "ok"
        assert [item["outcome"] for item in accounting.attempts] == [
            "failed",
            "success",
        ]
        assert [
            item["context"].attempt_index for item in accounting.attempts
        ] == [0, 1]
        assert accounting.attempts[0]["context"].request_origin == "rotor_agent"
        assert accounting.attempts[0]["context"].agent_run_id == "run-1"
        assert routing.unavailable == [1]

    async def test_internal_conversion_error_is_not_channel_error_fact(self) -> None:
        channel = SimpleNamespace(
            id=1,
            name="first",
            protocol="openai",
            model_mapping={},
        )
        accounting = RecordingAccounting()
        FakeAdapterFactory.adapters = {1: FailingConversionAdapter()}

        async def available_channels(model, token, db):
            return [channel]

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        chat_endpoint.routing_engine = FakeRoutingEngine([channel])
        chat_endpoint.AdapterFactory = FakeAdapterFactory
        chat_endpoint.get_available_channels = available_channels

        request = ChatCompletionRequest.model_validate(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "client": ("127.0.0.1", 1),
            }
        )

        with self.assertRaisesRegex(RuntimeError, "response conversion failed"):
            await chat_endpoint.chat_completions(
                request,
                http_request,
                Response(),
                FakeDatabase(),
                SimpleNamespace(id=1),
            )

        assert accounting.attempts == []

    async def test_success_attempt_is_recorded_before_usage_accounting_failure(
        self,
    ) -> None:
        accounting = FailingSuccessAccounting()
        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        request = ChatCompletionRequest.model_validate(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "client": ("127.0.0.1", 1),
            }
        )

        with self.assertRaisesRegex(RuntimeError, "usage accounting failed"):
            await chat_endpoint._handle_non_streaming_request(
                request,
                SuccessfulAdapter(),
                SimpleNamespace(id=2, protocol="openai"),
                SimpleNamespace(id=1),
                FakeDatabase(),
                0.0,
                http_request,
                "req-1",
                SimpleNamespace(conversation_id="conv-1"),
                attempt_context=AttemptContext.start(0),
            )

        assert accounting.attempts[0]["outcome"] == "success"

    async def test_stream_completion_records_successful_attempt(self) -> None:
        accounting = RecordingAccounting()
        conversation_store = FakeConversationStore()
        token = SimpleNamespace(id=1)
        session_maker = FakeSessionMaker(token)
        request_db = FakeDatabase()
        http_client = FakeHttpClient()

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = conversation_store
        chat_endpoint.async_session_maker = session_maker

        request = ChatCompletionRequest.model_validate(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "client": ("127.0.0.1", 1),
            }
        )
        channel = SimpleNamespace(id=2, protocol="openai")

        response = await chat_endpoint._handle_streaming_request(
            request,
            SuccessfulStreamingAdapter(),
            channel,
            token,
            request_db,
            0.0,
            http_request,
            http_client,
            "req-1",
            SimpleNamespace(conversation_id="conv-1"),
            response=object(),
            attempt_context=AttemptContext.start(0),
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert chunks[-1] == "data: [DONE]\n\n"
        assert accounting.attempts[0]["outcome"] == "success"
        assert request_db.closed is True
        assert http_client.closed is True

    async def test_stream_interruption_records_failed_attempt(self) -> None:
        accounting = RecordingAccounting()
        token = SimpleNamespace(id=1)
        request_db = FakeDatabase()
        http_client = FakeHttpClient()
        routing = FakeRoutingEngine([])

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        chat_endpoint.async_session_maker = FakeSessionMaker(token)
        chat_endpoint.routing_engine = routing

        request = ChatCompletionRequest.model_validate(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "client": ("127.0.0.1", 1),
            }
        )
        channel = SimpleNamespace(id=2, protocol="openai")

        response = await chat_endpoint._handle_streaming_request(
            request,
            FailingStreamingAdapter(),
            channel,
            token,
            request_db,
            0.0,
            http_request,
            http_client,
            "req-1",
            SimpleNamespace(conversation_id="conv-1"),
            response=object(),
            attempt_context=AttemptContext.start(0),
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert '"type": "stream_error"' in chunks[-1]
        assert accounting.attempts[0]["outcome"] == "failed"
        assert accounting.attempts[0]["error"].code == "upstream_timeout"
        assert accounting.attempts[0]["error"].phase == "provider_stream"
        assert routing.unavailable == [2]
        assert http_client.closed is True
