import asyncio
from copy import deepcopy
import json

from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport, MockTransport
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import rotor.api.admin.channels as channels_api
import rotor.api.v1.anthropic as anthropic_api
from rotor.adapters.factory import AdapterFactory
from rotor.api.v1.anthropic import anthropic_to_openai_request
from rotor.channels.presets import join_api_url
from rotor.database import Base, get_db
from rotor.models.channel import Channel
from rotor.schemas.request import AnthropicCountTokensRequest, AnthropicMessageRequest
from tests.test_attempt_latency import environment
from tests.test_recovery_endpoints import invoke


ROOT = "https://open.bigmodel.cn/api/anthropic"


@pytest.mark.parametrize("protocol", ["anthropic", "anthropic_messages"])
@pytest.mark.parametrize("operation", ["models", "messages"])
@pytest.mark.parametrize("base_suffix", ["", "/", "/v1", "/v1/", "/v1/messages", "/v1/messages/", "/v1/models"])
@pytest.mark.parametrize("path_prefix", ["/", "/v1/"])
def test_standard_anthropic_endpoints_share_one_versioned_root(protocol, operation, base_suffix, path_prefix):
    assert join_api_url(ROOT + base_suffix, path_prefix + operation, protocol=protocol) == f"{ROOT}/v1/{operation}"


@pytest.mark.parametrize("base,path,expected", [
    (ROOT, "messages/", ROOT + "/v1/messages"),
    (ROOT + "/v2", "/models", ROOT + "/v2/models"),
    (ROOT + "/v2/messages", "/models", ROOT + "/v2/models"),
    (ROOT + "/v2", "/v1/messages", ROOT + "/v1/messages"),
    (ROOT + "/v1/models", "/v2/messages", ROOT + "/v2/messages"),
    (ROOT, "/v2/models", ROOT + "/v2/models"),
    ("https://api.anthropic.com", "/messages", "https://api.anthropic.com/v1/messages"),
    (ROOT, "https://custom.example/messages?version=custom", "https://custom.example/messages?version=custom"),
    (ROOT, "/custom/messages", ROOT + "/custom/messages"),
    (ROOT + "/v1", "custom/models", ROOT + "/v1/custom/models"),
    (ROOT, "/api/anthropic/custom/models", ROOT + "/custom/models"),
    (ROOT, "/v2/custom/models", ROOT + "/v2/custom/models"),
])
def test_explicit_versions_and_custom_paths_are_respected(base, path, expected):
    assert join_api_url(base, path, protocol="anthropic") == expected


@pytest.mark.parametrize("protocol", [None, "openai", "openai_chat", "responses", "openai_responses", "custom"])
@pytest.mark.parametrize("base,path,expected", [
    ("https://example.test", "/models", "https://example.test/models"),
    ("https://example.test/v1", "/v1/models", "https://example.test/v1/models"),
    ("https://example.test/v1", "/responses", "https://example.test/v1/responses"),
    ("https://example.test/v1", "/chat/completions", "https://example.test/v1/chat/completions"),
    ("https://example.test/v1", "/images/generations", "https://example.test/v1/images/generations"),
])
def test_other_protocols_preserve_existing_join(base, path, expected, protocol):
    assert join_api_url(base, path, protocol=protocol) == expected


def test_admin_channel_lifecycle_preserves_config_and_normalizes_all_model_probes(monkeypatch):
    seen = []

    async def upstream(request):
        seen.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer mock-key"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "glm-5.3"}]})

    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        app = FastAPI()
        app.include_router(channels_api.router, prefix="/api/admin")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        payload = {
            "name": "zhipu-anthropic", "type": "zhipu", "protocol": "anthropic",
            "base_url": ROOT, "key": "mock-key", "models": ["glm-5.3"],
            "extra": {"request_path": "/messages", "models_path": "/models", "auth_type": "bearer"},
        }
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                unsaved = await client.post("/api/admin/channels/probe-models", json=payload)
                assert unsaved.status_code == 200 and unsaved.json()["models"] == ["glm-5.3"]
                created = await client.post("/api/admin/channels", json=payload)
                assert created.status_code == 201
                channel_id = created.json()["id"]
                assert created.json()["base_url"] == ROOT
                prefix = f"/api/admin/channels/{channel_id}"
                fetched = await client.get(prefix)
                assert fetched.status_code == 200 and fetched.json()["base_url"] == ROOT
                # An already saved unversioned channel works before any edit.
                saved = await client.post(prefix + "/probe-models")
                assert saved.status_code == 200 and saved.json()["models"] == ["glm-5.3"]
                tested = await client.post(prefix + "/test")
                assert tested.status_code == 200 and tested.json()["ok"]
                updated_base = ROOT + "/v1/messages/"
                updated = await client.put(prefix, json={"base_url": updated_base})
                assert updated.status_code == 200 and updated.json()["base_url"] == updated_base
                reprobed = await client.post(prefix + "/probe-models")
                assert reprobed.status_code == 200 and reprobed.json()["models"] == ["glm-5.3"]
                async with sessions() as db:
                    persisted = await db.get(Channel, channel_id)
                    assert persisted.base_url == updated_base
                    assert persisted.extra["auth_type"] == "bearer"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    assert seen == [("GET", ROOT + "/v1/models")] * 4


@pytest.mark.parametrize("protocol", ["anthropic", "anthropic_messages"])
@pytest.mark.parametrize("base_suffix", ["", "/v1/", "/v1/models"])
@pytest.mark.parametrize("stream", [False, True])
def test_generation_and_count_tokens_use_same_root_and_bearer(environment, monkeypatch, protocol, base_suffix, stream):
    env = environment
    env.channels[:] = env.channels[:1]
    channel = env.channels[0]
    channel.type = "zhipu"
    channel.protocol = protocol
    channel.base_url = ROOT + base_suffix
    channel.extra = {"request_path": "/messages", "models_path": "/models", "auth_type": "bearer"}
    original = (channel.base_url, deepcopy(channel.extra))
    seen = []

    async def upstream(request):
        seen.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer provider-key"
        assert "x-api-key" not in request.headers
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 3})
        payload = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "model",
                   "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 3, "output_tokens": 1}}
        if stream:
            events = [
                {"type": "message_start", "message": {**payload, "stop_reason": None}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join(f"data: {json.dumps(event)}\n\n" for event in events))
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(anthropic_api, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))

    async def exercise():
        result = await invoke(env, "anthropic", stream=stream)
        if stream:
            chunks = [chunk async for chunk in result.body_iterator]
            assert any("event: message_stop\n" in chunk for chunk in chunks)
            assert not any("event: error\n" in chunk for chunk in chunks)
        else:
            assert result["content"] == [{"type": "text", "text": "ok"}]
        count_request = AnthropicCountTokensRequest(model="model", messages=[{"role": "user", "content": "hi"}])
        http_request = Request({"type": "http", "method": "POST", "path": "/v1/messages/count_tokens", "headers": []})
        count_result = await anthropic_api.count_message_tokens(count_request, http_request, env.db, env.token)
        assert json.loads(count_result.body) == {"input_tokens": 3}

    asyncio.run(exercise())
    assert seen == [("POST", ROOT + "/v1/messages"), ("POST", ROOT + "/v1/messages/count_tokens")]
    assert (channel.base_url, channel.extra) == original


def test_zhipu_openai_channel_native_anthropic_override_is_normalized(environment):
    channel = environment.channels[0]
    channel.type = "zhipu"
    channel.protocol = "openai"
    channel.extra = {"anthropic_base_url": ROOT}
    seen = []

    async def upstream(request):
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer provider-key"
        return httpx.Response(200, json={"type": "message", "content": []})

    async def exercise():
        async with AsyncClient(transport=MockTransport(upstream)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            request = anthropic_to_openai_request(AnthropicMessageRequest(
                model="model", max_tokens=10, messages=[{"role": "user", "content": "hi"}],
            ))
            response = await adapter.make_request(request)
            assert response.status_code == 200

    asyncio.run(exercise())
    assert seen == [ROOT + "/v1/messages"]
    assert channel.extra == {"anthropic_base_url": ROOT}
