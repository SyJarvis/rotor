import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport, MockTransport
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.api.admin.channels as channels_api
from rotor.database import Base, get_db
from rotor.models.channel import Channel


def fetch(monkeypatch, replies, **options):
    seen = []
    async def upstream(request):
        seen.append(request)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)
    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    result = asyncio.run(channels_api._fetch_model_list(
        base_url="https://example.test/api/anthropic", key="stored-secret",
        provider_type="zhipu", protocol="anthropic", **options,
    ))
    return result, seen


@pytest.mark.parametrize("more,last", [("has_more", "last_id"), ("hasMore", "lastId")])
def test_paginated_catalog_is_complete_sorted_and_keeps_query(monkeypatch, more, last):
    models, requests = fetch(monkeypatch, [
        {"data": [{"id": "z"}, {"id": "a"}], more: True, last: "a", "next_url": "https://untrusted.example/steal"},
        {"data": [{"id": "b"}, {"id": "a"}], more: False, last: "b"},
    ], models_path="https://catalog.example/custom?tenant=stable")
    assert models == ["a", "b", "z"]
    assert [str(request.url) for request in requests] == [
        "https://catalog.example/custom?tenant=stable",
        "https://catalog.example/custom?tenant=stable&after_id=a",
    ]
    assert all(request.headers["Authorization"] == "Bearer stored-secret" for request in requests)
    assert all("x-api-key" not in request.headers for request in requests)


@pytest.mark.parametrize("payload", [
    {"data": [{"id": "b"}, {"id": "a"}, "b"]},
    {"models": [{"name": "b"}, "a", "b"]}, ["b", {"id": "a"}, "b"],
])
def test_openai_catalog_shapes_remain_supported(monkeypatch, payload):
    assert fetch(monkeypatch, [payload])[0] == ["a", "b"]


@pytest.mark.parametrize("payload", [{"data": []}, {"models": []}, [], {"data": [], "hasMore": False}])
def test_explicit_empty_catalog_is_success(monkeypatch, payload):
    assert fetch(monkeypatch, [payload])[0] == []


@pytest.mark.parametrize("payload", [
    {}, {"error": {"message": "stored-secret"}}, {"data": [], "success": False},
    {"data": [], "code": 401}, {"type": "error", "data": []}, {"data": None},
    {"data": [{}]}, {"data": [False]}, {"data": [{"id": 3}]}, {"data": [""]},
    {"data": ["a"], "has_more": "true"},
    {"data": ["a"], "has_more": True, "hasMore": False},
])
def test_business_errors_or_malformed_catalogs_never_succeed(monkeypatch, payload):
    with pytest.raises(ValueError, match="catalog"):
        fetch(monkeypatch, [payload])


@pytest.mark.parametrize("following", [
    {"data": ["b"], "has_more": True},
    {"data": ["b"], "has_more": True, "last_id": "a"},
    {"data": [], "has_more": True, "last_id": "b"},
    {"data": ["b"], "has_more": True, "last_id": 2},
    {"error": "stored-secret"}, httpx.Response(503, text="stored-secret"),
    httpx.ReadTimeout("upstream timed out"), httpx.Response(200, text="not json"),
])
def test_subsequent_page_failures_do_not_return_partial_success(monkeypatch, following):
    with pytest.raises((ValueError, httpx.HTTPError)):
        fetch(monkeypatch, [{"data": ["a"], "has_more": True, "last_id": "a"}, following])


@pytest.mark.parametrize("limit_name,limit,replies", [
    ("_MODEL_CATALOG_MAX_PAGES", 1, [{"data": ["a"], "has_more": True, "last_id": "a"}]),
    ("_MODEL_CATALOG_MAX_ITEMS", 1, [{"data": ["a", "a"]}]),
])
def test_catalog_bounds_fail_closed(monkeypatch, limit_name, limit, replies):
    monkeypatch.setattr(channels_api, limit_name, limit)
    with pytest.raises(ValueError, match="limit"):
        fetch(monkeypatch, replies)


@asynccontextmanager
async def admin_client():
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
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        await engine.dispose()


def test_saved_draft_probe_uses_stored_key_and_current_connection_without_persisting(monkeypatch):
    requests = []
    async def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "found"}]})
    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    async def exercise():
        async with admin_client() as (client, sessions):
            created = await client.post("/api/admin/channels", json={
                "name": "stored", "type": "openai", "protocol": "openai", "key": "stored-secret",
                "base_url": "https://old.example/v1", "models": ["original"],
                "extra": {"models_path": "/models", "request_path": "/chat/completions", "auth_type": "bearer"},
            })
            assert created.status_code == 201
            channel_id = created.json()["id"]
            prefix = f"/api/admin/channels/{channel_id}"
            original = (await client.get(prefix)).json()
            draft = {"base_url": "https://new.example/api/anthropic", "type": "zhipu", "protocol": "anthropic",
                     "extra": {"models_path": "/models", "auth_type": "bearer", "headers": {"X-Draft": "yes"},
                               "unrelated_option": "must-not-persist"},
                     "key": "ignored-replacement", "models": ["must-not-persist"], "enabled": False}
            probed = await client.post(prefix + "/probe-models", json=draft)
            assert probed.status_code == 200 and probed.json()["models"] == ["found"]
            assert set(probed.json()) == {"models", "latency_ms", "raw_count"}
            assert "secret" not in probed.text and "new.example" not in probed.text
            assert (await client.get(prefix)).json() == original
            async with sessions() as db:
                saved = await db.get(Channel, channel_id)
                assert saved.key == "stored-secret" and saved.models == ["original"] and saved.enabled
                assert saved.extra == original["extra"]
            old_probe = await client.post(prefix + "/probe-models")
            assert old_probe.status_code == 200
            # A replacement credential is deliberately sent only to the unsaved endpoint.
            draft["key"] = "replacement-secret"
            unsaved = await client.post("/api/admin/channels/probe-models", json=draft)
            assert unsaved.status_code == 200
            saved_result = await client.put(prefix, json={"protocol": "anthropic", "type": "zhipu"})
            assert saved_result.json()["extra"]["request_path"] == "/messages"
            assert saved_result.json()["extra"]["auth_type"] == "bearer"
            deleted = await client.delete(prefix)
            assert deleted.status_code == 204
            assert (await client.get(prefix)).status_code == 404
    asyncio.run(exercise())
    assert [str(request.url) for request in requests] == [
        "https://new.example/api/anthropic/v1/models", "https://old.example/v1/models",
        "https://new.example/api/anthropic/v1/models",
    ]
    assert requests[0].headers["Authorization"] == "Bearer stored-secret"
    assert requests[0].headers["X-Draft"] == "yes"
    assert requests[1].headers["Authorization"] == "Bearer stored-secret"
    assert "X-Draft" not in requests[1].headers
    assert requests[2].headers["Authorization"] == "Bearer replacement-secret"


def test_failed_paginated_probe_cannot_supply_partial_models_to_overwrite_configuration(monkeypatch):
    async def upstream(request):
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": ["partial"], "hasMore": True, "lastId": "partial"})
        return httpx.Response(200, json={"success": False, "message": "stored-secret", "data": []})
    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    async def exercise():
        async with admin_client() as (client, sessions):
            created = await client.post("/api/admin/channels", json={"name": "safe", "type": "zhipu", "protocol": "anthropic",
                "base_url": "https://example.test", "key": "stored-secret", "models": ["original"]})
            prefix = f"/api/admin/channels/{created.json()['id']}"
            original = deepcopy((await client.get(prefix)).json())
            result = await client.post(prefix + "/probe-models")
            assert result.status_code == 400
            assert "models" not in result.json() and "stored-secret" not in result.text
            assert (await client.get(prefix)).json() == original
            tested = await client.post(prefix + "/test")
            assert tested.status_code == 200 and not tested.json()["ok"]
            assert tested.json()["models"] == []
    asyncio.run(exercise())
