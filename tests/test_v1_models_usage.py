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
from rotor.models.admin_auth import AdminUser
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
async def discovery_client(*, allowed_channels=None, allowed_models=None, quota=None, used_quota=0,
                           zhipu_type="zhipu", owner_channels=None, owner_models=None, owned=False):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        if owned:
            db.add(AdminUser(id=1, username="owner", password_hash="unused", enabled=True,
                             must_change_password=False, quota=2000, used_quota=123,
                             allowed_channels=owner_channels, allowed_models=owner_models))
        db.add_all([
            Channel(id=1, name="codex", type="openai", protocol="openai", auth_type="oauth",
                    key="", base_url="https://chatgpt.com/backend-api/codex",
                    models=["gpt-6"], model_mapping={}, priority=10,
                    extra={"model_info": {"gpt-6": {
                        "display_name": "GPT-6", "max_input_tokens": 272000, "max_tokens": 128000,
                    }}}),
            Channel(id=2, name="zhipu", type=zhipu_type, protocol="openai",
                    key="provider-secret", base_url="https://open.bigmodel.cn/api/coding/paas/v4",
                    models=["glm-6"], model_mapping={"glm-alias": "glm-6"}, priority=1,
                    extra={}),
            Channel(id=3, name="disabled", type="openai", protocol="openai", enabled=False,
                    key="disabled-secret", base_url="https://disabled.example", models=["hidden"], model_mapping={}),
            Token(key=KEY, name="discovery", enabled=True, allowed_channels=allowed_channels,
                  allowed_models=allowed_models, owner_id=1 if owned else None,
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
            client.sessions = sessions
            yield client
    finally:
        usage_cache._entries.clear()
        await engine.dispose()


async def _store(client, channel_id, usage, *, now=None):
    async with client.sessions() as db:
        channel = await db.get(Channel, channel_id)
        usage_cache.store(channel_id, usage, channel=channel, now=now)


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
            await _store(client, 1, _usage(plan="plus"), now=time.time())
            await _store(client, 3, _usage(limit_reached=True, plan="pro"), now=time.time())
            # stale snapshots are not exposed
            await _store(client, 2, _usage(), now=time.time() - usage_cache.CACHE_TTL_SECONDS - 1)
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
            await _store(client, 2, _usage(plan="pro"), now=time.time())
            result = await client.get("/v1/usage", headers=AUTH)
        payload = result.json()
        assert payload["quota"] is None
        entry = next(item for item in payload["channels"] if item["channel_id"] == 2)
        assert entry["provider"] == "zhipu"
    asyncio.run(exercise())


def test_usage_reports_missing_subscription_without_inventing_windows():
    async def exercise():
        async with discovery_client() as client:
            await _store(client, 2, ChannelUsageResponse(
                subscription_status="inactive", allowed=False, fetched_at=time.time(),
            ))
            result = await client.get("/v1/usage", headers=AUTH)
        assert result.status_code == 200
        entry = result.json()["channels"][0]
        assert entry["provider"] == "zhipu"
        assert entry["subscription_status"] == "inactive"
        assert entry["plan"] is None
        assert entry["allowed"] is False
        assert entry["limit_reached"] is None
        assert entry["rate_limit"] == {"primary_window": None, "secondary_window": None}
    asyncio.run(exercise())


@pytest.mark.parametrize("provider_type", ["openai", "anthropic", "command"])
def test_usage_omits_old_zhipu_snapshot_for_wrong_selected_provider(provider_type):
    async def exercise():
        async with discovery_client(zhipu_type=provider_type) as client:
            await _store(client, 2, _usage(plan="pro"))
            result = await client.get("/v1/usage", headers=AUTH)
            assert result.status_code == 200
            assert result.json()["channels"] == []
    asyncio.run(exercise())


@pytest.mark.parametrize(("models", "expected"), [
    (None, [1, 2]), ([], []), (["gpt-6"], [1]), (["glm-alias"], [2]),
    (["glm-6"], [2]), (["GLM-6"], []), (["missing"], []),
])
def test_usage_restricts_model_names_and_aliases(models, expected):
    async def exercise():
        async with discovery_client(allowed_models=models) as client:
            for cid in (1, 2, 3):
                await _store(client, cid, _usage())
            result = await client.get("/v1/usage", headers=AUTH)
            assert result.status_code == 200
            assert [item["channel_id"] for item in result.json()["channels"]] == expected
    asyncio.run(exercise())


@pytest.mark.parametrize(("owner_channels", "owner_models", "key_channels", "key_models", "expected"), [
    ([1, 2], ["glm-alias"], [1, 2], ["glm-alias", "gpt-6"], [2]),
    ([1], ["glm-alias"], [1, 2], ["glm-alias"], []),
    ([1, 2], ["glm-alias"], [1, 2], ["gpt-6"], []),
    ([], None, None, None, []),
    (None, [], None, None, []),
    (None, None, [], None, []),
])
def test_usage_intersects_owner_and_token_permissions(owner_channels, owner_models, key_channels, key_models, expected):
    async def exercise():
        async with discovery_client(owned=True, owner_channels=owner_channels, owner_models=owner_models,
                                    allowed_channels=key_channels, allowed_models=key_models) as client:
            for cid in (1, 2):
                await _store(client, cid, _usage())
            result = await client.get("/v1/usage", headers=AUTH)
            assert result.status_code == 200
            assert result.json()["quota"] == {"used": 123, "limit": 2000, "unit": "tokens"}
            assert [item["channel_id"] for item in result.json()["channels"]] == expected
    asyncio.run(exercise())


def test_usage_unrestricted_models_keeps_empty_channel_compatible():
    async def exercise():
        async with discovery_client() as client:
            async with client.sessions() as db:
                channel = await db.get(Channel, 2)
                channel.models, channel.model_mapping = [], {}
                await db.commit()
            await _store(client, 2, _usage())
            assert [entry["channel_id"] for entry in (await client.get("/v1/usage", headers=AUTH)).json()["channels"]] == [2]
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [("key", "new-secret"), ("base_url", "https://elsewhere.test"),
                                         ("enabled", False), ("models", ["different-model"])])
def test_usage_rejects_cache_after_channel_identity_changes(field, value):
    async def exercise():
        async with discovery_client() as client:
            await _store(client, 2, _usage())
            async with client.sessions() as db:
                channel = await db.get(Channel, 2)
                setattr(channel, field, value)
                await db.commit()
            assert (await client.get("/v1/usage", headers=AUTH)).json()["channels"] == []
    asyncio.run(exercise())


def test_usage_rejects_unbound_cached_snapshots():
    async def exercise():
        async with discovery_client() as client:
            usage_cache.store(2, _usage())
            response = await client.get("/v1/usage", headers=AUTH)
            assert response.status_code == 200 and response.json()["channels"] == []
    asyncio.run(exercise())
