"""Confirmed subscription loss disables only Coding Plan channels persistently."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin import channels
from rotor.core.deps import get_available_channels
from rotor.database import Base, get_db
from rotor.gateway.routing import RoutingEngine
from rotor.models.channel import Channel
from rotor.schemas.channel import ChannelUsageResponse
from rotor.services import quota_refresher, usage_cache
from rotor.services.zhipu_quota import ZhipuQuotaError, is_zhipu_coding_channel


MISSING = {"code": 500, "success": False, "msg": "当前用户不存在coding plan", "data": None}
PAID = {"success": True, "data": {"level": "pro", "limits": [
    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 5},
]}}
CODING_URL = "https://open.bigmodel.cn/api/coding/paas/v4"


@asynccontextmanager
async def environment(tmp_path, monkeypatch, *, base_url=CODING_URL, enabled=True, provider_type="zhipu"):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'subscription.db'}"
    engine = create_async_engine(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            Channel(id=1, name="coding", type=provider_type, auth_type="api_key", key="old-key",
                    base_url=base_url, enabled=enabled, models=["glm"], model_mapping={}, extra={}),
            Channel(id=2, name="fallback", type="openai", key="fallback-key", base_url="https://example.com/v1",
                    enabled=True, models=["glm"], model_mapping={}, extra={}),
        ])
        await db.commit()
    state = {"reply": MISSING, "requests": [], "before_reply": None}

    async def upstream(request):
        state["requests"].append(request)
        if state["before_reply"]:
            await state["before_reply"]()
        reply = state["reply"]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    monkeypatch.setattr(channels.httpx, "AsyncClient", lambda **kw: AsyncClient(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    monkeypatch.setattr(quota_refresher, "async_session_maker", sessions)
    app = FastAPI()
    app.include_router(channels.router, prefix="/api/admin")

    async def db_dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    usage_cache._entries.clear()
    try:
        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(client=client, sessions=sessions, state=state, db_url=db_url)
    finally:
        usage_cache._entries.clear()
        await engine.dispose()


@pytest.mark.parametrize("path", ["admin", "background"])
@pytest.mark.parametrize("base_url", [CODING_URL, "https://open.bigmodel.cn/api/anthropic"])
def test_missing_subscription_disables_persistently_and_excludes_routing(tmp_path, monkeypatch, path, base_url):
    async def exercise():
        async with environment(tmp_path, monkeypatch, base_url=base_url) as env:
            if path == "admin":
                response = await env.client.get("/api/admin/channels/1/oauth/usage")
                assert response.status_code == 200
                assert response.json()["channel_enabled"] is False
            else:
                await quota_refresher.QuotaRefresher()._refresh(1)
            async with env.sessions() as db:
                assert (await db.get(Channel, 1)).enabled is False
                assert (await db.get(Channel, 2)).enabled is True
                assert [c.id for c in await get_available_channels("glm", db=db)] == [2]
                all_channels = (await db.scalars(select(Channel))).all()
                assert [c.id for c in RoutingEngine().route(all_channels, "glm").candidates] == [2]
            db_url = env.db_url
        restarted = create_async_engine(db_url)
        try:
            async with async_sessionmaker(restarted)() as db:
                assert (await db.get(Channel, 1)).enabled is False
        finally:
            await restarted.dispose()
    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["admin", "background"])
@pytest.mark.parametrize("reply", [
    httpx.Response(401, json=MISSING), httpx.Response(503, json=MISSING),
    {"success": False, "code": 500, "msg": "系统异常"},
    {"success": True, "data": {"limits": []}}, httpx.ReadTimeout("timeout"),
])
def test_failures_never_disable_channels(tmp_path, monkeypatch, path, reply):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            env.state["reply"] = reply
            if path == "admin":
                assert (await env.client.get("/api/admin/channels/1/oauth/usage")).status_code >= 500
            else:
                with pytest.raises(ZhipuQuotaError):
                    await quota_refresher.QuotaRefresher()._refresh(1)
            async with env.sessions() as db:
                assert (await db.get(Channel, 1)).enabled is True
    asyncio.run(exercise())


def test_payg_and_oauth_channels_are_not_disabled(tmp_path, monkeypatch):
    async def exercise():
        async with environment(tmp_path, monkeypatch, base_url="https://open.bigmodel.cn/api/paas/v4") as env:
            response = await env.client.get("/api/admin/channels/1/oauth/usage")
            assert response.status_code == 200 and response.json()["channel_enabled"] is True
            async with env.sessions() as db:
                channel = await db.get(Channel, 1)
                channel.auth_type = "oauth"
                channel.base_url = CODING_URL
                await db.commit()
                await quota_refresher.apply_subscription_status(db, channel, ChannelUsageResponse(
                    subscription_status="inactive", fetched_at=1,
                ))
                assert channel.enabled is True
    asyncio.run(exercise())


def test_renewal_does_not_enable_until_explicit_successful_enable(tmp_path, monkeypatch):
    async def exercise():
        async with environment(tmp_path, monkeypatch, enabled=False) as env:
            assert (await env.client.post("/api/admin/channels/1/enable")).status_code == 409
            env.state["reply"] = httpx.ReadTimeout("timeout")
            assert (await env.client.post("/api/admin/channels/1/enable")).status_code == 503
            env.state["reply"] = PAID
            renewed = await env.client.get("/api/admin/channels/1/oauth/usage?refresh=1")
            assert renewed.status_code == 200 and renewed.json()["channel_enabled"] is False
            assert renewed.json()["plan"] == "pro"
            async with env.sessions() as db:
                assert (await db.get(Channel, 1)).enabled is False
            enabled = await env.client.post("/api/admin/channels/1/enable")
            assert enabled.status_code == 200 and enabled.json()["enabled"] is True
            assert usage_cache.fresh(1) is None
    asyncio.run(exercise())


def test_config_update_invalidates_old_subscription_snapshot(tmp_path, monkeypatch):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            assert (await env.client.get("/api/admin/channels/1/oauth/usage")).status_code == 200
            assert usage_cache.fresh(1).subscription_status == "inactive"
            env.state["reply"] = PAID
            updated = await env.client.put("/api/admin/channels/1", json={"key": "new-key", "enabled": True})
            assert updated.status_code == 200 and usage_cache.fresh(1) is None
            env.state["reply"] = PAID
            response = await env.client.get("/api/admin/channels/1/oauth/usage")
            assert response.status_code == 200 and response.json()["channel_enabled"] is True
            assert env.state["requests"][-1].headers["Authorization"] == "new-key"
    asyncio.run(exercise())


@pytest.mark.parametrize("reply,status", [(MISSING, 409), (httpx.ReadTimeout("timeout"), 503)])
def test_edit_cannot_enable_missing_subscription_or_save_unverified_new_credentials(tmp_path, monkeypatch, reply, status):
    async def exercise():
        async with environment(tmp_path, monkeypatch, enabled=False) as env:
            env.state["reply"] = reply
            result = await env.client.put("/api/admin/channels/1", json={"enabled": True, "key": "new-key"})
            assert result.status_code == status
            async with env.sessions() as db:
                channel = await db.get(Channel, 1)
                assert channel.enabled is False and channel.key == "old-key"
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [("key", "new-key"), ("type", "openai")])
def test_inflight_old_configuration_cannot_disable_updated_channel(tmp_path, monkeypatch, field, value):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            async def replace_credentials():
                async with env.sessions() as db:
                    channel = await db.get(Channel, 1)
                    setattr(channel, field, value)
                    await db.commit()
            env.state["before_reply"] = replace_credentials
            response = await env.client.get("/api/admin/channels/1/oauth/usage")
            assert response.status_code == 503
            assert usage_cache.fresh(1) is None
            async with env.sessions() as db:
                channel = await db.get(Channel, 1)
                assert channel.enabled is True and getattr(channel, field) == value
    asyncio.run(exercise())


@pytest.mark.parametrize("provider_type", ["openai", "anthropic", "command"])
@pytest.mark.parametrize("path", ["admin", "background", "apply"])
def test_wrong_provider_never_queries_or_disables_even_with_old_cache(tmp_path, monkeypatch, provider_type, path):
    async def exercise():
        async with environment(tmp_path, monkeypatch, provider_type=provider_type) as env:
            missing = ChannelUsageResponse(subscription_status="inactive", fetched_at=1)
            usage_cache.store(1, missing)
            if path == "admin":
                response = await env.client.get("/api/admin/channels/1/oauth/usage")
                assert response.status_code == 400
                assert usage_cache.fresh(1) is None
            elif path == "background":
                await quota_refresher.QuotaRefresher()._refresh(1)
            else:
                async with env.sessions() as db:
                    await quota_refresher.apply_subscription_status(db, await db.get(Channel, 1), missing)
            assert env.state["requests"] == []
            async with env.sessions() as db:
                assert (await db.get(Channel, 1)).enabled is True
    asyncio.run(exercise())


@pytest.mark.parametrize("url,expected", [
    (CODING_URL, True), ("https://api.z.ai/api/anthropic/", True),
    ("https://open.bigmodel.cn/api/paas/v4", False),
    ("https://open.bigmodel.cn/api/anthropic-other", False),
    ("https://open.bigmodel.cn.example.com/api/coding/paas/v4", False),
])
def test_only_official_coding_endpoints_can_be_auto_disabled(url, expected):
    assert is_zhipu_coding_channel(SimpleNamespace(type="zhipu", auth_type="api_key", base_url=url)) is expected


def test_frontend_entry_html_requires_cache_revalidation():
    from rotor.main import app

    async def exercise():
        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/", "/index.html"):
                response = await client.get(path)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-cache"
    asyncio.run(exercise())
