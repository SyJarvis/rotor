import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI
import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from rotor.api import client_oauth
from rotor.api.admin import auth, channels as admin_channels
from rotor.api.control import channels as control_channels
from rotor.api.v1 import anthropic, anthropic_models, chat, models, responses
from rotor.config import OAuthClientSettings, settings
from rotor.core.admin_auth import ADMIN_CSRF_COOKIE, hash_admin_password, require_admin_access
from rotor.core.control_auth import ControlAPIException, control_api_exception_handler
from rotor.core.exceptions import APIRouterException, api_router_exception_handler
from rotor import database
from rotor.database import Base, create_database_engine, get_db
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.client_oauth import OAuthCredential, OAuthGrant
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


API_KEY = "sk-alice-default-key"
OTHER_KEY = "sk-alice-other-key"
CALLBACK = "http://127.0.0.1:1455/auth/callback"
VERIFIER = "v" * 64
PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/anthropic/v1/messages"}


class SSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        chunk = {"id": "chatcmpl-oauth", "object": "chat.completion.chunk", "created": 123, "model": "model-a"}
        for choices, usage in [
            ([{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}], None),
            ([{"index": 0, "delta": {}, "finish_reason": "stop"}], None),
            ([], {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}),
        ]:
            payload = {**chunk, "choices": choices}
            if usage is not None:
                payload["usage"] = usage
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"


@asynccontextmanager
async def gateway(tmp_path, monkeypatch, *, upstream_wait=None, with_api_keys=True):
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            AdminUser(id=1, username="alice", role="user", password_hash=hash_admin_password("login-password"),
                      enabled=True, must_change_password=False, quota=1000, allowed_channels=[1],
                      allowed_models=["model-a"], default_token_id=11 if with_api_keys else None),
            AdminUser(id=2, username="bob", role="user", password_hash="unused", enabled=True,
                      must_change_password=False, quota=1000, allowed_channels=[2], default_token_id=21 if with_api_keys else None),
            *([Token(id=11, owner_id=1, name="base", key=API_KEY, enabled=True, is_base=True,
                  allowed_channels=[1, 2], allowed_models=["model-a", "hidden-model", "channel-secret"]),
            Token(id=12, owner_id=1, name="other", key=OTHER_KEY, enabled=True),
            Token(id=21, owner_id=2, name="base", key="sk-bob-default-key", enabled=True)] if with_api_keys else []),
            Channel(id=1, name="allowed", type="openai", protocol="openai", key="upstream-key",
                    base_url="https://provider.invalid/v1", models=["model-a", "hidden-model"]),
            Channel(id=2, name="restricted", type="openai", protocol="openai", key="other-upstream-key",
                    base_url="https://restricted.invalid/v1", models=["channel-secret"]),
        ])
        await db.commit()

    monkeypatch.setattr(settings, "ROTOR_ADMIN_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "ROTOR_OAUTH_CLIENTS", {"rotor-client": OAuthClientSettings(
        name="Rotor Client", redirect_uris=["http://127.0.0.1/auth/callback", "http://[::1]/auth/callback"])})
    monkeypatch.setattr(settings, "ROTOR_CONTROL_API_TOKEN", "control-only-key")
    monkeypatch.setattr(settings, "CONVERSATION_STORE_ENABLED", False)
    monkeypatch.setattr(settings, "CONVERSATION_STORE_DIR", str(tmp_path / "conversations"))
    monkeypatch.setattr(database, "async_session_maker", sessions)
    sent = []

    async def upstream(request):
        sent.append(request)
        assert request.url.host == "provider.invalid"
        assert request.headers["authorization"] == "Bearer upstream-key"
        body = json.loads(request.content)
        assert body["model"] == "model-a"
        if upstream_wait is not None:
            await upstream_wait()
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SSEStream())
        return httpx.Response(200, json={
            "id": "chatcmpl-oauth", "object": "chat.completion", "created": 123, "model": "model-a",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    routing = RoutingEngine("fallback_order")
    routing.protocol_affinity_enabled = False
    for module in (chat, responses, anthropic):
        monkeypatch.setattr(module, "async_session_maker", sessions)
        monkeypatch.setattr(module, "routing_engine", routing)
        monkeypatch.setattr(module, "accounting_service", AccountingService())
        monkeypatch.setattr(module, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

    app = FastAPI()
    app.add_exception_handler(APIRouterException, api_router_exception_handler)
    app.add_exception_handler(ControlAPIException, control_api_exception_handler)
    app.include_router(auth.router, prefix="/api")
    app.include_router(client_oauth.router)
    for module in (chat, responses, models):
        app.include_router(module.router, prefix="/v1")
    for module in (anthropic, anthropic_models):
        app.include_router(module.router, prefix="/anthropic/v1")
    app.include_router(admin_channels.router, prefix="/api/admin", dependencies=[Depends(require_admin_access)])
    app.include_router(control_channels.router, prefix="/api/control/v1")

    async def db_dependency():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = db_dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            login = await browser.post("/api/auth/login", json={"username": "alice", "password": "login-password"})
            assert login.status_code == 200, login.text
            assert login.json()["default_token_id"] == (11 if with_api_keys else None)
            csrf = browser.cookies[ADMIN_CSRF_COOKIE]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
            authorization = await browser.post("/api/oauth/authorization", json={
                "response_type": "code", "client_id": "rotor-client", "redirect_uri": CALLBACK,
                "scope": "inference", "state": "current-client-login", "code_challenge": challenge,
                "code_challenge_method": "S256", "approve": True,
            }, headers={"X-CSRF-Token": csrf})
            assert authorization.status_code == 200, authorization.text
            callback = parse_qs(urlsplit(authorization.json()["redirect_url"]).query)
            assert callback["state"] == ["current-client-login"]
            code = callback["code"][0]
            exchange = await browser.post("/oauth/token", data={
                "grant_type": "authorization_code", "client_id": "rotor-client", "code": code,
                "redirect_uri": CALLBACK, "code_verifier": VERIFIER,
            })
            assert exchange.status_code == 200, exchange.text
            credentials = exchange.json()
            assert credentials["token_type"] == "Bearer" and credentials["scope"] == "inference"
            assert API_KEY not in exchange.text and OTHER_KEY not in exchange.text
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield SimpleNamespace(client=client, browser=browser, sessions=sessions, sent=sent,
                                      credentials=credentials, code=code, csrf=csrf,
                                      headers={"Authorization": f"Bearer {credentials['access_token']}"})
    finally:
        await engine.dispose()


async def infer(env, entrypoint="chat", *, stream=False, model="model-a", headers=None):
    payload = {"model": model, "stream": stream}
    if entrypoint == "responses":
        payload.update(input="hello", store=False)
    else:
        payload["messages"] = [{"role": "user", "content": "hello"}]
        if entrypoint == "anthropic":
            payload["max_tokens"] = 20
    return await env.client.post(PATHS[entrypoint], json=payload, headers=env.headers if headers is None else headers)


@pytest.mark.parametrize("entrypoint", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_api_keys", [False, True])
def test_browser_login_bearer_reaches_all_protocols_and_owned_accounting(tmp_path, monkeypatch, entrypoint, stream, with_api_keys):
    async def exercise():
        async with gateway(tmp_path, monkeypatch, with_api_keys=with_api_keys) as env:
            response = await infer(env, entrypoint, stream=stream)
            assert response.status_code == 200, response.text
            assert "hello" in response.text
            if stream:
                assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[entrypoint] in response.text
                assert "stream_error" not in response.text and "response.failed" not in response.text
            assert len(env.sent) == 1
            assert env.credentials["access_token"] not in str(env.sent[0].headers)
            async with env.sessions() as db:
                user, token = await db.get(AdminUser, 1), await db.get(Token, 11)
                ledger = (await db.scalars(select(UsageLedger))).one()
                log = (await db.scalars(select(RequestLog))).one()
                assert (ledger.owner_id, ledger.token_id, ledger.channel_id) == (1, None, 1)
                assert (log.owner_id, log.token_id, log.channel_id) == (1, None, 1)
                assert ledger.request_protocol == {"chat": "openai_chat", "responses": "openai_responses", "anthropic": "anthropic_messages"}[entrypoint]
                assert ledger.total_tokens == 6 and ledger.usage_source == "provider" and log.success
                assert user.used_quota == 6
                if with_api_keys:
                    assert (token.used_quota, token.request_count) == (0, 0)
                    assert (await db.get(Token, 12)).used_quota == 0
                else:
                    assert (await db.scalars(select(Token))).all() == []

    asyncio.run(exercise())


def test_slow_oauth_inference_does_not_block_model_discovery_or_other_account_writes(tmp_path, monkeypatch):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def upstream_wait():
            started.set()
            await release.wait()

        async with gateway(tmp_path, monkeypatch, upstream_wait=upstream_wait) as env:
            async def other_account_write():
                async with env.sessions() as db:
                    await db.execute(update(AdminUser).where(AdminUser.id == 2).values(display_name="updated while waiting"))
                    await db.commit()

            pending = asyncio.create_task(infer(env))
            concurrent = None
            try:
                await asyncio.wait_for(started.wait(), timeout=3)
                concurrent = asyncio.gather(env.client.get("/v1/models", headers=env.headers), other_account_write())
                discovery, _ = await asyncio.wait_for(asyncio.shield(concurrent), timeout=1)
                assert discovery.status_code == 200, discovery.text
                assert not pending.done() and not release.is_set()
                async with env.sessions() as db:
                    assert (await db.get(AdminUser, 2)).display_name == "updated while waiting"
            finally:
                release.set()
                tasks = [pending] if concurrent is None else [pending, concurrent]
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
            response = await pending
            assert response.status_code == 200, response.text
            async with env.sessions() as db:
                assert (await db.get(AdminUser, 1)).used_quota == 6
                assert (await db.get(Token, 11)).used_quota == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("limit", ["account_channels", "key_channels", "account_models", "key_models"])
def test_model_discovery_and_three_protocols_use_account_permissions_and_keep_key_limits_separate(tmp_path, monkeypatch, limit):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(
                    allowed_channels=[1] if limit == "account_channels" else None,
                    allowed_models=["model-a"] if limit == "account_models" else None))
                await db.execute(update(Token).where(Token.id == 11).values(
                    allowed_channels=[] if limit == "key_channels" else None,
                    allowed_models=[] if limit == "key_models" else None))
                await db.commit()
            hidden = "channel-secret" if limit.endswith("channels") else "hidden-model"
            for path in ("/v1/models", "/anthropic/v1/models"):
                discovery = await env.client.get(path, headers=env.headers)
                assert discovery.status_code == 200, discovery.text
                visible = {item["id"] for item in discovery.json()["data"]}
                assert "model-a" in visible
                assert (hidden not in visible) is limit.startswith("account_")
            if limit.startswith("account_"):
                for entrypoint in PATHS:
                    forbidden = await infer(env, entrypoint, model=hidden)
                    assert forbidden.status_code == 404, forbidden.text
                assert env.sent == []
            else:
                for entrypoint in PATHS:
                    forbidden_key = await infer(env, entrypoint, headers={"Authorization": "Bearer " + API_KEY})
                    assert forbidden_key.status_code == 404, forbidden_key.text
                    allowed_account = await infer(env, entrypoint)
                    assert allowed_account.status_code == 200, allowed_account.text
            async with env.sessions() as db:
                assert (await db.get(Token, 11)).used_quota == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("first_auth", ["oauth", "api_key"])
def test_oauth_and_api_key_debit_one_shared_account_quota_without_default_key_binding(tmp_path, monkeypatch, first_auth):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(default_token_id=12))
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(quota=12))
                await db.commit()
            oauth_headers, api_headers = env.headers, {"x-api-key": API_KEY}
            ordered = [oauth_headers, api_headers] if first_auth == "oauth" else [api_headers, oauth_headers]
            for headers in ordered:
                success = await infer(env, "anthropic", headers=headers)
                assert success.status_code == 200, success.text
            for headers in (env.headers, {"Authorization": f"Bearer {API_KEY}"}):
                blocked = await infer(env, headers=headers)
                assert blocked.status_code == 429, blocked.text
            assert len(env.sent) == 2
            async with env.sessions() as db:
                assert (await db.get(AdminUser, 1)).used_quota == 12
                assert (await db.get(Token, 11)).used_quota == 6
                assert (await db.get(Token, 12)).used_quota == 0
                assert {row.token_id for row in (await db.scalars(select(UsageLedger))).all()} == {11, None}

    asyncio.run(exercise())


def test_temporarily_exhausted_account_quota_does_not_consume_refresh_credential(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            quota_model, identity = AdminUser, 1
            async with env.sessions() as db:
                await db.execute(update(quota_model).where(quota_model.id == identity).values(quota=0))
                await db.commit()
            form = {"grant_type": "refresh_token", "client_id": "rotor-client", "refresh_token": env.credentials["refresh_token"]}
            unavailable = await env.client.post("/oauth/token", data=form)
            assert unavailable.status_code == 429, unavailable.text
            assert unavailable.json()["error"] == "temporarily_unavailable"
            assert (await infer(env)).status_code == 429
            assert env.sent == []
            async with env.sessions() as db:
                await db.execute(update(quota_model).where(quota_model.id == identity).values(quota=1000))
                await db.commit()
            refreshed = await env.client.post("/oauth/token", data=form)
            assert refreshed.status_code == 200, refreshed.text
            headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
            assert (await infer(env, headers=headers)).status_code == 200

    asyncio.run(exercise())


@pytest.mark.parametrize("key_change", ["disabled", "expired", "deleted", "owner_changed", "quota_exhausted", "default_changed"])
def test_key_lifecycle_does_not_change_account_oauth_inference_or_refresh(tmp_path, monkeypatch, key_change):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                if key_change == "deleted":
                    await db.execute(delete(Token).where(Token.id == 11))
                elif key_change == "default_changed":
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(default_token_id=12))
                else:
                    values = {"disabled": {"enabled": False}, "expired": {"expire_time": datetime.now(timezone.utc) - timedelta(seconds=1)},
                              "owner_changed": {"owner_id": 2}, "quota_exhausted": {"quota": 0, "enabled": False}}[key_change]
                    await db.execute(update(Token).where(Token.id == 11).values(**values))
                await db.commit()
            for path in ("/v1/models", "/anthropic/v1/models"):
                discovery = await env.client.get(path, headers=env.headers)
                assert discovery.status_code == 200, discovery.text
                assert {item["id"] for item in discovery.json()["data"]} == {"model-a"}
            for entrypoint in PATHS:
                success = await infer(env, entrypoint)
                assert success.status_code == 200, success.text
            refreshed = await env.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client",
                "refresh_token": env.credentials["refresh_token"]})
            assert refreshed.status_code == 200, refreshed.text
            async with env.sessions() as db:
                assert (await db.get(AdminUser, 1)).used_quota == 18
                assert {row.token_id for row in (await db.scalars(select(UsageLedger))).all()} == {None}
                token = await db.get(Token, 11)
                assert token is None or token.used_quota == 0

    asyncio.run(exercise())


def test_real_key_quota_exhaustion_does_not_disable_account_oauth(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                await db.execute(update(Token).where(Token.id == 11).values(quota=6))
                await db.commit()
            key_headers = {"Authorization": "Bearer " + API_KEY}
            assert (await infer(env, headers=key_headers)).status_code == 200
            async with env.sessions() as db:
                key = await db.get(Token, 11)
                assert key.used_quota == 6 and key.enabled is False
            assert (await infer(env, headers=key_headers)).status_code == 401
            assert (await infer(env)).status_code == 200
            async with env.sessions() as db:
                assert (await db.get(AdminUser, 1)).used_quota == 12
                assert (await db.get(Token, 11)).used_quota == 6
                assert {row.token_id for row in (await db.scalars(select(UsageLedger))).all()} == {11, None}

    asyncio.run(exercise())


def test_account_without_keys_can_refresh_before_three_protocol_inference(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch, with_api_keys=False) as env:
            refreshed = await env.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client",
                "refresh_token": env.credentials["refresh_token"]})
            assert refreshed.status_code == 200, refreshed.text
            headers = {"Authorization": "Bearer " + refreshed.json()["access_token"]}
            for entrypoint in PATHS:
                assert (await infer(env, entrypoint, headers=headers)).status_code == 200
            async with env.sessions() as db:
                assert (await db.scalars(select(Token))).all() == []
                assert (await db.get(AdminUser, 1)).used_quota == 18

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["revoke", "access_expired", "grant_expired", "user_disabled", "password_changed", "client_removed"])
def test_unavailable_authorization_rejects_inference_and_optional_model_discovery(tmp_path, monkeypatch, failure):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            if failure == "revoke":
                grants = await env.browser.get("/api/oauth/grants")
                assert grants.status_code == 200, grants.text
                grant_id = grants.json()["items"][0]["id"]
                revoked = await env.browser.delete(f"/api/oauth/grants/{grant_id}", headers={"X-CSRF-Token": env.csrf})
                assert revoked.status_code in (200, 204), revoked.text
            elif failure == "client_removed":
                monkeypatch.setattr(settings, "ROTOR_OAUTH_CLIENTS", {})
            else:
                async with env.sessions() as db:
                    past = datetime.now(timezone.utc) - timedelta(seconds=1)
                    if failure == "access_expired":
                        await db.execute(update(OAuthCredential).values(access_expires_at=past))
                    elif failure == "grant_expired":
                        await db.execute(update(OAuthGrant).values(expires_at=past))
                    elif failure == "user_disabled":
                        await db.execute(update(AdminUser).where(AdminUser.id == 1).values(enabled=False))
                    elif failure == "password_changed":
                        await db.execute(update(AdminUser).where(AdminUser.id == 1).values(password_hash="reset-password-hash"))
                    await db.commit()
            for entrypoint in PATHS:
                response = await infer(env, entrypoint)
                assert response.status_code in (401, 403), response.text
            for path in ("/v1/models", "/anthropic/v1/models"):
                response = await env.client.get(path, headers=env.headers)
                assert response.status_code in (401, 403), response.text
            assert env.sent == []

    asyncio.run(exercise())


def test_oauth_is_inference_only_and_other_credential_types_cannot_bypass_auth(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            for path in ("/api/admin/channels", "/api/control/v1/channels", "/api/oauth/grants"):
                response = await env.client.get(path, headers=env.headers)
                assert response.status_code in (401, 403), response.text
            for value in (env.code, env.credentials["refresh_token"]):
                for entrypoint in PATHS:
                    response = await infer(env, entrypoint, headers={"Authorization": f"Bearer {value}"})
                    assert response.status_code == 401, response.text
                discovery = await env.client.get("/v1/models", headers={"Authorization": f"Bearer {value}"})
                assert discovery.status_code == 401, discovery.text
            bearer_as_key = await infer(env, "anthropic", headers={"x-api-key": env.credentials["access_token"]})
            assert bearer_as_key.status_code == 401, bearer_as_key.text
            assert (await env.client.get("/v1/models", headers={"x-api-key": env.credentials["access_token"]})).status_code == 401
            assert env.sent == []
            assert (await infer(env, headers={"Authorization": f"Bearer {API_KEY}"})).status_code == 200

    asyncio.run(exercise())


def test_refresh_reuse_revokes_rotated_access_but_preserves_api_key(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            form = {"grant_type": "refresh_token", "client_id": "rotor-client", "refresh_token": env.credentials["refresh_token"]}
            refreshed = await env.client.post("/oauth/token", data=form)
            assert refreshed.status_code == 200, refreshed.text
            new_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
            assert (await env.client.get("/v1/models", headers=new_headers)).status_code == 200
            replay = await env.client.post("/oauth/token", data=form)
            assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
            for headers in (env.headers, new_headers):
                assert (await infer(env, headers=headers)).status_code in (401, 403)
                assert (await env.client.get("/v1/models", headers=headers)).status_code in (401, 403)
            assert env.sent == []
            assert (await infer(env, headers={"Authorization": f"Bearer {API_KEY}"})).status_code == 200

    asyncio.run(exercise())
