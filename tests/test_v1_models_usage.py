"""OpenAI-compatible discovery endpoints: /v1/models, /v1/models/{id}, /v1/usage."""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.v1.models import router as models_router
from rotor.api.v1.usage import router as usage_router
from rotor.config import settings
from rotor.database import Base, get_db
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.schemas.channel import ChannelUsageResponse, ChannelUsageWindow
from rotor.services import usage_cache


KEY = settings.API_KEY_PREFIX + "discovery-credential"
AUTH = {"Authorization": f"Bearer {KEY}"}


def _usage(limit_reached=False, plan="plus"):
    return ChannelUsageResponse(
        plan=plan,
        allowed=not limit_reached,
        limit_reached=limit_reached,
        primary=ChannelUsageWindow(used_percent=7.0, window_seconds=18000, reset_after_seconds=300, reset_at=1800000300.0),
        secondary=ChannelUsageWindow(used_percent=15.0, window_seconds=604800, reset_after_seconds=400000, reset_at=1800400300.0),
        fetched_at=1000.0,
    )


@asynccontextmanager
async def discovery_client(*, allowed_channels=None, quota=None, used_quota=0):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            Channel(id=1, name="codex", type="openai", protocol="openai", auth_type="oauth",
                    key="", base_url="https://chatgpt.com/backend-api/codex",
                    models=["gpt-6"], model_mapping={}, priority=10,
                    extra={"model_info": {"gpt-6": {
                        "display_name": "GPT-6", "max_input_tokens": 272000, "max_tokens": 128000,
                    }}}),
            Channel(id=2, name="zhipu", type="openai", protocol="openai",
                    key="provider-secret", base_url="https://open.bigmodel.cn/api/coding/paas/v4",
                    models=["glm-6"], model_mapping={"glm-alias": "glm-6"}, priority=1,
                    extra={}),
            Channel(id=3, name="disabled", type="openai", protocol="openai", enabled=False,
                    key="disabled-secret", base_url="https://disabled.example", models=["hidden"], model_mapping={}),
            Token(key=KEY, name="discovery", enabled=True, allowed_channels=allowed_channels,
                  quota=quota, used_quota=used_quota),
        ])
        await db.commit()
    usage_cache._entries.clear()
    app = FastAPI()
    app.include_router(models_router, prefix="/v1")
    app.include_router(usage_router, prefix="/v1")

    async def override_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        usage_cache._entries.clear()
        await engine.dispose()


@pytest.mark.parametrize("path", ["/v1/models", "/v1/models/gpt-6", "/v1/usage"])
def test_discovery_requires_authentication(path):
    async def exercise():
        async with discovery_client() as client:
            result = await client.get(path)
            assert result.status_code == 401
            assert "provider-secret" not in result.text
    asyncio.run(exercise())


def test_models_list_includes_aliases_and_channel_metadata():
    async def exercise():
        async with discovery_client() as client:
            result = await client.get("/v1/models", headers=AUTH)
        assert result.status_code == 200
        entries = {entry["id"]: entry for entry in result.json()["data"]}
        # mapping aliases are listed, disabled channels are not
        assert sorted(entries) == ["glm-6", "glm-alias", "gpt-6"]
        assert all(entry["owned_by"] == "rotor" for entry in entries.values())
        # metadata comes from the highest-priority channel carrying it
        assert entries["gpt-6"]["display_name"] == "GPT-6"
        assert entries["gpt-6"]["max_input_tokens"] == 272000
        assert entries["gpt-6"]["max_tokens"] == 128000
        # channels without model_info stay minimal
        assert entries["glm-6"]["display_name"] is None
    asyncio.run(exercise())


def test_models_list_respects_token_channel_scope():
    async def exercise():
        async with discovery_client(allowed_channels=[2]) as client:
            result = await client.get("/v1/models", headers=AUTH)
        entries = {entry["id"] for entry in result.json()["data"]}
        assert entries == {"glm-6", "glm-alias"}
    asyncio.run(exercise())


def test_model_retrieve_hides_out_of_scope_models():
    async def exercise():
        async with discovery_client(allowed_channels=[2]) as client:
            visible = await client.get("/v1/models/glm-6", headers=AUTH)
            hidden = await client.get("/v1/models/gpt-6", headers=AUTH)
            unknown = await client.get("/v1/models/nope", headers=AUTH)
        assert visible.status_code == 200
        assert visible.json()["id"] == "glm-6"
        # out-of-scope and unknown are indistinguishable
        assert hidden.status_code == 404
        assert unknown.status_code == 404
    asyncio.run(exercise())


def test_usage_reports_token_quota_and_cached_windows():
    async def exercise():
        async with discovery_client(allowed_channels=[1, 2], quota=1000, used_quota=200) as client:
            usage_cache.store(1, _usage(plan="plus"), now=time.time())
            usage_cache.store(3, _usage(limit_reached=True, plan="pro"), now=time.time())
            # stale snapshots are not exposed
            usage_cache.store(2, _usage(), now=time.time() - usage_cache.CACHE_TTL_SECONDS - 1)
            result = await client.get("/v1/usage", headers=AUTH)
        assert result.status_code == 200
        payload = result.json()
        assert payload["quota"] == {"used": 200, "limit": 1000, "unit": "tokens"}
        # channel 3 is out of scope, channel 2 is stale: only channel 1 remains
        assert [entry["channel_id"] for entry in payload["channels"]] == [1]
        entry = payload["channels"][0]
        assert entry["channel"] == "codex"
        assert entry["provider"] == "chatgpt"
        assert entry["plan"] == "plus"
        assert entry["rate_limit"]["primary_window"]["used_percent"] == 7.0
        assert entry["rate_limit"]["secondary_window"]["window_seconds"] == 604800
        assert entry["limit_reached"] is False
    asyncio.run(exercise())


def test_usage_reports_zhipu_provider_and_unlimited_quota():
    async def exercise():
        async with discovery_client() as client:
            usage_cache.store(2, _usage(plan="pro"), now=time.time())
            result = await client.get("/v1/usage", headers=AUTH)
        payload = result.json()
        assert payload["quota"] is None
        entry = next(item for item in payload["channels"] if item["channel_id"] == 2)
        assert entry["provider"] == "zhipu"
    asyncio.run(exercise())
