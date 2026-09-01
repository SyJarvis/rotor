import asyncio
import unittest
from types import SimpleNamespace

import httpx
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.chat as chat_endpoint
from rotor.core.exceptions import ChannelException, UpstreamOverloaded
from rotor.gateway.accounting import AccountingService, UsageData
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
        self.finishes = []

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
        self.finishes.append((status, latency_ms))


class RecordingAccounting:
    def __init__(self) -> None:
        self.attempts = []
        self.successes = []

    def record_routing_decision(self, db, **kwargs) -> None:
        return None

    async def record(self, **kwargs) -> bool:
        self.attempts.append(kwargs)
        kwargs["context"].recorded = True
        return True

    async def record_failure(self, db, **kwargs) -> None:
        return None

    async def record_success(self, db, **kwargs) -> None:
        self.successes.append(kwargs)

    async def record_lease_success(self, db, **kwargs) -> None:
        return None

    def extract_usage(self, response_data) -> UsageData:
        return UsageData()

    def streaming_usage(self, **kwargs) -> UsageData:
        return UsageData()


class UsageRecordingAccounting(RecordingAccounting):
    def __init__(self) -> None:
        super().__init__()
        self.service = AccountingService()

    def extract_usage(self, response_data) -> UsageData:
        return self.service.extract_usage(response_data)

    def streaming_usage(self, **kwargs) -> UsageData:
        return self.service.streaming_usage(**kwargs)


class FailingSuccessAccounting(RecordingAccounting):
    async def record_success(self, db, **kwargs) -> None:
        raise RuntimeError("usage accounting failed")


class FakeRoutingEngine:
    def __init__(self, channels) -> None:
        self.channels = channels
        self.unavailable = []
        self.route_kwargs = None

    def route(self, channels, **kwargs):
        self.route_kwargs = kwargs
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


class DetailedUsageStreamingAdapter:
    async def stream_convert_response(self, response, request):
        yield {
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 0},
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 40,
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


class GenericFailingStreamingAdapter:
    async def stream_convert_response(self, response, request):
        if False:
            yield {}
        raise RuntimeError("response conversion failed")

    def map_model_name(self, model):
        return f"provider-{model}"


class CancelledStreamingAdapter:
    async def stream_convert_response(self, response, request):
        if False:
            yield {}
        raise asyncio.CancelledError

    def map_model_name(self, model):
        return f"provider-{model}"


class OverloadedStreamingAdapter:
    async def stream_convert_response(self, response, request):
        yield {
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}]
        }
        raise UpstreamOverloaded(
            "Our servers are currently overloaded. Please try again later.",
            error_type="overloaded_error",
        )

    def map_model_name(self, model):
        return f"provider-{model}"


class ErrorChunkStreamingAdapter:
    async def stream_convert_response(self, response, request):
        yield {
            "error": {
                "type": "overloaded_error",
                "message": "Upstream is overloaded, retry later",
            }
        }

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
            "get_preferred_channel_id": chat_endpoint.get_preferred_channel_id,
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

    async def test_stable_session_reads_and_commits_session_lease(self) -> None:
        channel = SimpleNamespace(
            id=2,
            name="leased",
            protocol="openai",
            model_mapping={},
        )
        accounting = RecordingAccounting()
        routing = FakeRoutingEngine([channel])
        FakeAdapterFactory.adapters = {2: SuccessfulAdapter()}

        async def available_channels(model, token, db):
            return [channel]

        async def preferred_channel(db, **kwargs):
            assert kwargs == {
                "token_id": 9,
                "session_id": "session-a",
                "logical_model": "model-a",
            }
            return 2

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        chat_endpoint.routing_engine = routing
        chat_endpoint.AdapterFactory = FakeAdapterFactory
        chat_endpoint.get_available_channels = available_channels
        chat_endpoint.get_preferred_channel_id = preferred_channel

        request = ChatCompletionRequest.model_validate({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
        })
        http_request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"x-rotor-session-id", b"session-a")],
            "client": ("127.0.0.1", 1),
        })

        await chat_endpoint.chat_completions(
            request,
            http_request,
            Response(),
            FakeDatabase(),
            SimpleNamespace(id=9),
        )

        assert routing.route_kwargs["preferred_channel_id"] == 2
        assert accounting.successes[0]["lease_session_id"] == "session-a"
        assert accounting.successes[0]["lease_migration_reason"] == (
            "request_success"
        )

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

    async def test_stream_uses_latest_coherent_input_usage_snapshot(self) -> None:
        accounting = UsageRecordingAccounting()
        token = SimpleNamespace(id=1)
        request_db = FakeDatabase()
        http_client = FakeHttpClient()

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = FakeConversationStore()
        chat_endpoint.async_session_maker = FakeSessionMaker(token)

        request = ChatCompletionRequest.model_validate({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        http_request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1),
        })

        response = await chat_endpoint._handle_streaming_request(
            request,
            DetailedUsageStreamingAdapter(),
            SimpleNamespace(id=2, protocol="openai"),
            token,
            request_db,
            0.0,
            http_request,
            http_client,
            "req-cache-snapshot",
            SimpleNamespace(conversation_id="conv-1"),
            response=object(),
            attempt_context=AttemptContext.start(0),
        )
        [chunk async for chunk in response.body_iterator]

        usage = accounting.successes[0]["usage"]
        assert usage.prompt_tokens == 120
        assert usage.cached_tokens == 80
        assert usage.uncached_input_tokens == 40

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

    async def test_generic_stream_failure_records_terminal_attempt_without_lease(
        self,
    ) -> None:
        accounting = RecordingAccounting()
        conversation_store = FakeConversationStore()
        token = SimpleNamespace(id=1)
        request_db = FakeDatabase()
        http_client = FakeHttpClient()

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = conversation_store
        chat_endpoint.async_session_maker = FakeSessionMaker(token)

        request = ChatCompletionRequest.model_validate({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        http_request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1),
        })

        response = await chat_endpoint._handle_streaming_request(
            request,
            GenericFailingStreamingAdapter(),
            SimpleNamespace(id=2, protocol="openai"),
            token,
            request_db,
            0.0,
            http_request,
            http_client,
            "req-generic-failure",
            SimpleNamespace(conversation_id="conv-1"),
            response=object(),
            attempt_context=AttemptContext.start(0),
            lease_session_id="session-a",
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert '"type": "stream_error"' in chunks[-1]
        assert accounting.attempts[0]["outcome"] == "failed"
        assert accounting.attempts[0]["error"].code == "upstream_unknown_error"
        assert accounting.successes == []
        assert conversation_store.finishes[-1][0] == "failed"
        assert http_client.closed is True

    async def test_cancelled_stream_records_cancelled_attempt_without_lease(
        self,
    ) -> None:
        accounting = RecordingAccounting()
        conversation_store = FakeConversationStore()
        token = SimpleNamespace(id=1)
        request_db = FakeDatabase()
        http_client = FakeHttpClient()

        chat_endpoint.accounting_service = accounting
        chat_endpoint.attempt_recorder = accounting
        chat_endpoint.conversation_store = conversation_store
        chat_endpoint.async_session_maker = FakeSessionMaker(token)

        request = ChatCompletionRequest.model_validate({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        http_request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1),
        })

        response = await chat_endpoint._handle_streaming_request(
            request,
            CancelledStreamingAdapter(),
            SimpleNamespace(id=2, protocol="openai"),
            token,
            request_db,
            0.0,
            http_request,
            http_client,
            "req-cancelled",
            SimpleNamespace(conversation_id="conv-1"),
            response=object(),
            attempt_context=AttemptContext.start(0),
            lease_session_id="session-a",
        )

        with self.assertRaises(asyncio.CancelledError):
            [chunk async for chunk in response.body_iterator]

        assert accounting.attempts[0]["outcome"] == "cancelled"
        assert accounting.successes == []
        assert conversation_store.finishes[-1][0] == "cancelled"
        assert http_client.closed is True

    async def test_stream_overload_marks_channel_unavailable_and_records_fact(self) -> None:
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
            OverloadedStreamingAdapter(),
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
        assert accounting.attempts[0]["error"].code == "upstream_overloaded"
        assert accounting.attempts[0]["error"].fallback_allowed is True
        assert accounting.attempts[0]["error"].retry_same_channel is False
        assert accounting.attempts[0]["error"].phase == "provider_stream"
        # The routing layer now sees mid-stream overload and cools the channel.
        assert routing.unavailable == [2]
        assert http_client.closed is True

    async def test_openai_stream_error_chunk_triggers_routing_self_healing(self) -> None:
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
            ErrorChunkStreamingAdapter(),
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
        assert accounting.attempts[0]["error"].code == "upstream_overloaded"
        assert routing.unavailable == [2]
        assert http_client.closed is True

    async def test_stream_request_with_all_channels_failing_closes_http_client(self) -> None:
        channel = SimpleNamespace(
            id=1,
            name="only",
            protocol="openai",
            model_mapping={},
        )
        accounting = RecordingAccounting()
        routing = FakeRoutingEngine([channel])
        http_client = FakeHttpClient()
        FakeAdapterFactory.adapters = {1: FailingAdapter()}

        async def available_channels(model, token, db):
            return [channel]

        original_async_client = chat_endpoint.AsyncClient
        chat_endpoint.AsyncClient = lambda *args, **kwargs: http_client
        try:
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

            with self.assertRaises(ChannelException):
                await chat_endpoint.chat_completions(
                    request,
                    http_request,
                    Response(),
                    FakeDatabase(),
                    SimpleNamespace(id=1),
                )
        finally:
            chat_endpoint.AsyncClient = original_async_client

        # The streaming generator never started, so the endpoint must have
        # closed the client itself instead of leaking it.
        assert http_client.closed is True
        assert routing.unavailable == [1]
