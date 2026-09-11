import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.anthropic as anthropic_endpoint
import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.images as images_endpoint
import rotor.api.v1.responses as responses_endpoint
import rotor.gateway.attempts as attempts_module
import rotor.gateway.routing as routing_module
from rotor.core.exceptions import ChannelException
from rotor.gateway.accounting import AccountingService, UsageData
from rotor.gateway.attempts import AttemptContext
from rotor.gateway.routing import RoutingEngine
from rotor.models.log import RequestLog
from rotor.models.usage import UsageLedger
from rotor.schemas.request import AnthropicMessageRequest, ChatCompletionRequest
from rotor.schemas.responses import ResponsesRequest
from rotor.services.session_leases import SessionLeasePreference


class Clock:
    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class Database:
    def __init__(self, token, clock):
        self.token = token
        self.clock = clock
        self.added = []
        self.get_delay = 0

    def add(self, row):
        self.added.append(row)

    async def execute(self, statement):
        return SimpleNamespace(rowcount=1)

    async def get(self, model, key):
        self.clock.advance(self.get_delay)
        return self.token

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def environment(monkeypatch):
    clock = Clock()
    token = SimpleNamespace(id=1, user_id="user", allowed_channels=None)
    db = Database(token, clock)
    engine = RoutingEngine(strategy="fallback_order")
    accounting = AccountingService()
    channels = [
        SimpleNamespace(
            id=channel_id,
            name=f"channel-{channel_id}",
            enabled=True,
            type="openai",
            protocol="openai",
            base_url=f"https://channel-{channel_id}.example/v1",
            key="provider-key",
            models=["model"],
            model_mapping={},
            priority=1,
            weight=1,
            extra={},
        )
        for channel_id in (1, 2)
    ]
    store = SimpleNamespace(
        start=AsyncMock(return_value=SimpleNamespace(conversation_id="conv")),
        append_routing=AsyncMock(),
        append_response=AsyncMock(),
        append_usage=AsyncMock(),
        append_error=AsyncMock(),
        finish=AsyncMock(),
    )
    # Replace only each module's clock reference; asyncio keeps its real clock.
    monkeypatch.setattr(attempts_module, "time", clock)
    monkeypatch.setattr(routing_module, "routing_engine", engine)
    for endpoint in (chat_endpoint, anthropic_endpoint, responses_endpoint, images_endpoint):
        monkeypatch.setattr(endpoint, "time", clock)
        monkeypatch.setattr(endpoint, "routing_engine", engine)
        monkeypatch.setattr(endpoint, "accounting_service", accounting)
        monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=channels))
        monkeypatch.setattr(endpoint, "get_session_lease_preference", AsyncMock(return_value=SessionLeasePreference(None)))
        monkeypatch.setattr(endpoint, "async_session_maker", lambda: db)
        if hasattr(endpoint, "conversation_store"):
            monkeypatch.setattr(endpoint, "conversation_store", store)
    return SimpleNamespace(
        clock=clock, token=token, db=db, engine=engine,
        accounting=accounting, channels=channels,
    )


class CompletionStream(httpx.AsyncByteStream):
    def __init__(self, clock, fail, images):
        self.clock = clock
        self.fail = fail
        self.images = images

    async def __aiter__(self):
        self.clock.advance(0.05)
        first = (
            {"type": "image_generation.partial_image", "b64_json": "partial"}
            if self.images
            else {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
        )
        yield f"data: {json.dumps(first)}\n\n".encode()
        self.clock.advance(0.1)
        if self.fail:
            raise httpx.ReadError("stream interrupted")
        final = (
            {"type": "image_generation.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
            if self.images
            else {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        yield f"data: {json.dumps(final)}\n\n".encode()
        yield b"data: [DONE]\n\n"


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "responses", "images"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_fallback_scores_only_the_current_attempt(environment, monkeypatch, protocol, stream, fail):
    env = environment

    async def upstream(request):
        if request.url.host == "channel-1.example":
            env.clock.advance(120)
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        if stream:
            env.clock.advance(0.05)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=CompletionStream(env.clock, fail, protocol == "images"),
            )
        env.clock.advance(0.2)
        if fail:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={
            "choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)

    for endpoint in (chat_endpoint, anthropic_endpoint, responses_endpoint, images_endpoint):
        monkeypatch.setattr(endpoint, "AsyncClient", client_factory)

    http_request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "client": ("127.0.0.1", 123),
    })

    async def exercise():
        if protocol == "chat":
            result = await chat_endpoint.chat_completions(
                ChatCompletionRequest(model="model", messages=[{"role": "user", "content": "hi"}], stream=stream),
                http_request, Response(), env.db, env.token,
            )
        elif protocol == "anthropic":
            result = await anthropic_endpoint.messages(
                AnthropicMessageRequest(model="model", messages=[{"role": "user", "content": "hi"}], max_tokens=100, stream=stream),
                http_request, Response(), env.db, env.token,
            )
        elif protocol == "responses":
            result = await responses_endpoint.create_response(
                ResponsesRequest(model="model", input="hi", stream=stream),
                http_request, Response(), env.db, env.token,
            )
        else:
            result = await images_endpoint.generate_image(
                images_endpoint.ImageGenerationRequest(model="model", prompt="hi", size="1024x1024", stream=stream),
                http_request, Response(), env.db, env.token,
            )
        if stream:
            chunks = [chunk async for chunk in result.body_iterator]
            assert chunks

    if fail and not stream:
        with pytest.raises(ChannelException):
            asyncio.run(exercise())
    else:
        asyncio.run(exercise())

    first = env.engine.adaptive._stats[("model", 1)]
    second = env.engine.adaptive._stats[("model", 2)]
    assert first.failures == 1
    assert first.inflight == 0
    assert first.latency_ewma_ms == pytest.approx(120_000, abs=1)
    assert second.failures == int(fail)
    assert second.successes == int(not fail)
    assert second.inflight == 0
    assert second.latency_ewma_ms == pytest.approx(200, abs=1)
    logs = [row for row in env.db.added if isinstance(row, RequestLog) and row.channel_id == 2]
    ledgers = [row for row in env.db.added if isinstance(row, UsageLedger) and row.channel_id == 2]
    assert len(logs) == len(ledgers) == 1
    assert logs[0].latency == ledgers[0].latency_ms / 1000
    assert ledgers[0].latency_ms == pytest.approx(120_200, abs=1)


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("attempt_latency_ms", [None, 0, 200])
def test_accounting_observes_only_explicit_attempt_samples(environment, success, attempt_latency_ms):
    env = environment
    channel = env.channels[1]
    env.engine.begin_attempt("model", channel)
    kwargs = dict(
        request_id="req", conversation_id="conv", request_protocol="openai_chat",
        token=env.token, channel=channel, model="model", latency_ms=120_200,
        client_ip="127.0.0.1", attempt_latency_ms=attempt_latency_ms,
    )
    if success:
        asyncio.run(env.accounting.record_success(
            env.db, provider_model="model", usage=UsageData(), **kwargs,
        ))
    else:
        asyncio.run(env.accounting.record_failure(
            env.db, error_code="test_error", error_message="failed", **kwargs,
        ))

    stats = env.engine.adaptive._stats[("model", channel.id)]
    if attempt_latency_ms is None:
        assert stats.observations == 0
        assert stats.inflight == 1
        assert stats.latency_ewma_ms is None
    else:
        assert stats.observations == 1
        assert stats.inflight == 0
        assert stats.latency_ewma_ms == attempt_latency_ms
    assert next(row for row in env.db.added if isinstance(row, UsageLedger)).latency_ms == 120_200


def test_background_acceptance_is_scored_once_and_polling_does_not_release_another_attempt(environment):
    env = environment
    channel = env.channels[1]
    payload = {"id": "resp_1", "object": "response", "status": "queued", "output": [], "usage": None}

    class Adapter:
        async def make_request(self, request):
            env.clock.advance(0.2)
            return httpx.Response(200, json=payload)

        async def convert_response(self, response, request):
            return response.json()

        def map_model_name(self, model):
            return model

    async def exercise():
        context = AttemptContext.start(0)
        env.engine.begin_attempt("model", channel)
        await chat_endpoint._handle_non_streaming_request(
            ChatCompletionRequest(model="model", messages=[]),
            Adapter(), channel, env.token, env.db, env.clock.time(),
            Request({"type": "http", "headers": []}), "req",
            SimpleNamespace(conversation_id="conv"),
            request_protocol="openai_responses", attempt_context=context,
        )
        stats = env.engine.adaptive._stats[("model", channel.id)]
        assert stats.successes == 1
        assert stats.inflight == 0
        assert stats.latency_ewma_ms == pytest.approx(200, abs=1)
        assert not any(isinstance(row, UsageLedger) for row in env.db.added)
        env.engine.begin_attempt("model", channel)
        route = SimpleNamespace(id=1, usage_accounted=False, conversation_id="conv", model="model", created_at=context.started_at)
        for _ in range(2):
            await responses_endpoint._account_deferred_response_usage(
                env.db, route=route, channel=channel, token=env.token,
                payload={"id": "resp_1", "object": "response", "model": "model", "status": "completed", "output": [],
                         "usage": {"input_tokens": 1, "output_tokens": 1}},
                client_ip="127.0.0.1", latency_ms=15,
            )
        assert stats.successes == 1
        assert stats.inflight == 1
        assert stats.latency_ewma_ms == pytest.approx(200, abs=1)
        ledgers = [row for row in env.db.added if isinstance(row, UsageLedger)]
        assert len(ledgers) == 1
        assert ledgers[0].latency_ms == 15

    asyncio.run(exercise())
