import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import rotor.api.v1.anthropic as endpoint
from rotor.adapters.factory import AdapterFactory
from rotor.core.deps import get_available_channels
from rotor.core.exceptions import ChannelsTemporarilyUnavailable, ModelNotFoundException, UpstreamProtocolError
from rotor.database import Base, get_db
from rotor.gateway.routing import RoutingEngine
from rotor.models.channel import Channel
from rotor.schemas.request import AnthropicCountTokensRequest


def channel(channel_id=1, protocol="anthropic", provider_type="openai"):
    return SimpleNamespace(
        id=channel_id, name=f"channel-{channel_id}", type=provider_type,
        protocol=protocol, enabled=True, priority=10 - channel_id, weight=1,
        extra={}, model_mapping={"model": "provider-model"}, models=["provider-model"],
        base_url=f"https://provider-{channel_id}.example/v1", key="mock-key",
    )


@pytest.fixture
def state(monkeypatch):
    now = [100.0]
    engine = RoutingEngine("fallback_order", clock=lambda: now[0])
    channels = [channel(), channel(2)]
    available = AsyncMock(side_effect=lambda *args: list(channels))
    monkeypatch.setattr(endpoint, "get_available_channels", available)
    monkeypatch.setattr(endpoint, "routing_engine", engine)
    accounting = SimpleNamespace(record_success=AsyncMock(), record_failure=AsyncMock())
    leases = AsyncMock()
    monkeypatch.setattr(endpoint, "accounting_service", accounting)
    monkeypatch.setattr(endpoint, "get_session_lease_preference", leases)
    db = SimpleNamespace(commit=AsyncMock(), add=Mock(), execute=AsyncMock())
    token = SimpleNamespace(id=7, allowed_channels=None)
    seen = []
    clients = []
    handler = [lambda request: httpx.Response(200, json={"input_tokens": 7})]

    async def upstream(request):
        seen.append(request)
        result = handler[0](request)
        return await result if hasattr(result, "__await__") else result

    def create_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(endpoint, "AsyncClient", create_client)
    request = AnthropicCountTokensRequest(model="model", messages=[{"role": "user", "content": "hello"}])
    http_request = Request({
        "type": "http", "method": "POST", "path": "/anthropic/v1/messages/count_tokens",
        "headers": [(b"anthropic-version", b"2023-06-01"), (b"anthropic-beta", b"mock-beta"),
                    (b"authorization", b"Bearer client-secret")],
    })

    async def call():
        return await endpoint.count_message_tokens(request, http_request, db, token)

    result = SimpleNamespace(**locals())
    yield result
    accounting.record_success.assert_not_called()
    accounting.record_failure.assert_not_called()
    leases.assert_not_called()
    db.commit.assert_not_called()
    db.add.assert_not_called()
    assert all(client.is_closed for client in clients)


def test_native_count_routes_maps_model_forwards_beta_and_does_not_train(state):
    state.channels.reverse()
    response = asyncio.run(state.call())
    assert json.loads(response.body) == {"input_tokens": 7}
    assert response.headers["X-Rotor-Token-Count-Source"] == "provider"
    assert response.headers["X-Rotor-Channel"] == "channel-1"
    assert response.headers["X-Rotor-Provider-Model"] == "provider-model"
    request = state.seen[0]
    assert str(request.url) == "https://provider-1.example/v1/messages/count_tokens"
    assert json.loads(request.content)["model"] == "provider-model"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-beta"] == "mock-beta"
    assert "client-secret" not in str(request.headers)
    state.available.assert_awaited_once_with("model", state.token, state.db)
    for candidate in state.channels:
        score = state.engine.adaptive.score("model", candidate)
        assert score.observations == 0 and score.load_utility == 1


def test_zhipu_openai_adapter_uses_native_count_capability(state):
    state.channels[:] = [channel(protocol="openai", provider_type="zhipu")]
    state.channels[0].extra = {"anthropic_base_url": "https://zhipu.example/api/anthropic"}
    response = asyncio.run(state.call())
    assert response.headers["X-Rotor-Token-Count-Source"] == "provider"
    assert str(state.seen[0].url) == "https://zhipu.example/api/anthropic/v1/messages/count_tokens"
    assert state.seen[0].headers["authorization"] == "Bearer mock-key"
    assert state.seen[0].headers["anthropic-beta"] == "mock-beta"
    assert json.loads(state.seen[0].content)["model"] == "provider-model"


@pytest.mark.parametrize("status_code", [429, 503])
def test_retryable_native_failure_falls_back_and_respects_retry_after(state, status_code):
    state.handler[0] = lambda request: (
        httpx.Response(status_code, headers={"Retry-After": "61"}, json={"error": {"message": "retry"}})
        if request.url.host == "provider-1.example" else httpx.Response(200, json={"input_tokens": 0})
    )
    response = asyncio.run(state.call())
    assert json.loads(response.body) == {"input_tokens": 0}
    assert response.headers["X-Rotor-Fallback"] == "true"
    assert response.headers["X-Rotor-Token-Count-Source"] == "provider"
    assert [request.url.host for request in state.seen] == ["provider-1.example", "provider-2.example"]
    assert state.engine.retry_after_seconds("model", state.channels[:1]) == 61
    assert all(state.engine.adaptive.score("model", candidate).load_utility == 1 for candidate in state.channels)


@pytest.mark.parametrize("status_code,attempts", [(400, 1), (401, 2), (403, 2)])
def test_native_client_error_follows_existing_fallback_policy_without_estimation(state, status_code, attempts):
    state.handler[0] = lambda request: httpx.Response(status_code, json={"error": {"message": "invalid"}})
    with pytest.raises(httpx.HTTPStatusError) as captured:
        asyncio.run(state.call())
    assert captured.value.response.status_code == status_code
    assert len(state.seen) == attempts


@pytest.mark.parametrize("payload", [
    {"input_tokens": True}, {"input_tokens": False}, {"input_tokens": -1},
    {"input_tokens": 1.5}, {"input_tokens": "7"}, {}, [],
    {"input_tokens": 7, "error": {"message": "secret input"}},
    {"input_tokens": 7, "success": False},
    {"input_tokens": 7, "type": "error"},
])
def test_invalid_native_200_response_is_explicit_failure(state, payload):
    state.handler[0] = lambda request: httpx.Response(200, json=payload)
    with pytest.raises(UpstreamProtocolError) as captured:
        asyncio.run(state.call())
    assert captured.value.status_code == 502
    assert "secret input" not in str(captured.value)
    assert len(state.seen) == 1


def test_invalid_native_json_is_explicit_failure(state):
    state.handler[0] = lambda request: httpx.Response(200, content="not json")
    with pytest.raises(UpstreamProtocolError, match="invalid JSON"):
        asyncio.run(state.call())


def test_estimate_is_labeled_only_without_native_tokenizer(state):
    state.channels[:] = [channel(protocol="openai"), channel(2, "openai_responses")]
    result = asyncio.run(state.call())
    assert result.headers["X-Rotor-Token-Count-Source"] == "estimated"
    assert json.loads(result.body)["input_tokens"] > 0
    assert state.seen == []
    assert state.engine.adaptive._stats == {}


def test_native_cooldown_cannot_be_bypassed_by_estimation(state):
    for candidate in state.channels:
        state.engine.mark_unavailable("model", candidate, 17)
    state.channels.append(channel(3, "openai"))
    with pytest.raises(ChannelsTemporarilyUnavailable) as captured:
        asyncio.run(state.call())
    assert captured.value.status_code == 503
    assert captured.value.headers["Retry-After"] == "17"
    assert state.seen == []


def test_successful_count_does_not_recover_generation_probe_or_release_other_attempt(state):
    state.channels[:] = state.channels[:1]
    candidate = state.channels[0]
    healthy = state.engine.admit_attempt("model", candidate)
    state.engine.mark_unavailable("model", candidate, 0)
    response = asyncio.run(state.call())
    assert response.status_code == 200
    score = state.engine.adaptive.score("model", candidate)
    assert score.observations == 0 and score.load_utility == 0.5
    assert state.engine.admit_attempt("model", candidate) is None
    assert state.engine.retry_after_seconds("model", [candidate]) == 30
    state.now[0] += 30
    probe = state.engine.admit_attempt("model", candidate)
    assert probe.is_probe
    state.engine.release_attempt(probe)
    state.engine.release_attempt(healthy)
    assert state.engine.adaptive.score("model", candidate).load_utility == 1


def test_count_cancellation_releases_probe_and_http_client(state):
    state.channels[:] = state.channels[:1]
    candidate = state.channels[0]
    state.engine.mark_unavailable("model", candidate, 0)

    async def cancelled(request):
        raise asyncio.CancelledError()

    state.handler[0] = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(state.call())
    assert state.engine.adaptive.score("model", candidate).load_utility == 1
    assert state.engine.admit_attempt("model", candidate) is None
    assert state.engine.retry_after_seconds("model", [candidate]) == 30


def test_candidate_initialization_failure_does_not_block_other_native_count(state, monkeypatch):
    original = AdapterFactory.create_adapter

    def create_adapter(candidate, client):
        if candidate.id == 1:
            raise ValueError("bad channel configuration")
        return original(candidate, client)

    monkeypatch.setattr(AdapterFactory, "create_adapter", create_adapter)
    response = asyncio.run(state.call())
    assert response.headers["X-Rotor-Channel"] == "channel-2"
    assert len(state.seen) == 1


def test_admission_race_denial_never_sends_or_releases_unowned_attempt(state, monkeypatch):
    release = Mock()
    monkeypatch.setattr(state.engine, "admit_attempt", Mock(return_value=None))
    monkeypatch.setattr(state.engine, "release_attempt", release)
    with pytest.raises(ChannelsTemporarilyUnavailable):
        asyncio.run(state.call())
    assert state.seen == []
    release.assert_not_called()


def test_count_http_requires_authentication_before_channel_selection(state):
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/anthropic/v1")
    app.dependency_overrides[get_db] = lambda: state.db

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/anthropic/v1/messages/count_tokens", json={
                "model": "model", "messages": [{"role": "user", "content": "hi"}],
            })
            assert response.status_code == 401

    asyncio.run(exercise())
    state.available.assert_not_called()
    assert state.seen == []


def test_failed_native_candidates_never_fall_back_to_estimation(state):
    state.channels.append(channel(3, "openai"))
    state.handler[0] = lambda request: httpx.Response(503, json={"error": {"message": "busy"}})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(state.call())
    assert len(state.seen) == 2


def test_count_respects_real_channel_permissions_model_mapping_and_read_only_database(monkeypatch):
    seen = []
    monkeypatch.setattr(endpoint, "get_available_channels", get_available_channels)
    monkeypatch.setattr(endpoint, "routing_engine", RoutingEngine("fallback_order"))

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"input_tokens": 9})

    monkeypatch.setattr(endpoint, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs))

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                for number in (1, 2):
                    candidate = channel(number)
                    db.add(Channel(**vars(candidate)))
                await db.commit()
                token = SimpleNamespace(id=7, allowed_channels=[2])
                request = AnthropicCountTokensRequest(model="model", messages=[{"role": "user", "content": "hi"}])
                http_request = Request({"type": "http", "headers": []})
                response = await endpoint.count_message_tokens(request, http_request, db, token)
                assert response.headers["X-Rotor-Channel"] == "channel-2"
                assert not db.new and not db.dirty and not db.deleted
                denied = SimpleNamespace(id=8, allowed_channels=[99])
                with pytest.raises(ModelNotFoundException):
                    await endpoint.count_message_tokens(request, http_request, db, denied)
                request.model = "unavailable-model"
                with pytest.raises(ModelNotFoundException):
                    await endpoint.count_message_tokens(request, http_request, db, token)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    assert len(seen) == 1 and seen[0].url.host == "provider-2.example"
    assert json.loads(seen[0].content)["model"] == "provider-model"
