import asyncio
import base64
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient, MockTransport, Response
import pytest
from sqlalchemy import Select, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rotor.api.admin import channel_oauth, channels
from rotor.core.admin_auth import require_admin_access
from rotor.core.exceptions import normalize_upstream_error
from rotor.database import Base, create_database_engine, get_db
from rotor.main import channel_validation_exception_handler
from rotor.models.channel import Channel
from rotor.models.channel_oauth import ChannelOAuthSession
from rotor.schemas.channel import ChannelCreate, ChannelResponse, ChannelUpdate
from rotor.services import chatgpt_oauth as oauth


def jwt(account="account-1"):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}, "padding": "\uffff"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def tokens(account="account-1", refresh="refresh-secret", lifetime=3600):
    return {"access_token": jwt(account), "refresh_token": refresh, "expires_in": lifetime}


def channel_fields(**overrides):
    fields = dict(name="oauth-channel", type="openai_codex", auth_type="oauth", key="",
                  protocol="openai_responses", base_url=oauth.CODEX_BASE_URL, models=["gpt-codex"],
                  model_mapping={}, extra={}, oauth_access_token=jwt(), oauth_refresh_token="refresh-secret",
                  oauth_account_id="account-1", oauth_expires_at=time.time() + 3600, oauth_status="connected")
    fields.update(overrides)
    return fields


@asynccontextmanager
async def database(tmp_path, monkeypatch):
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(oauth, "async_session_maker", sessions)
    try:
        yield sessions
    finally:
        await engine.dispose()


async def seed(sessions, **overrides):
    async with sessions() as db:
        channel = Channel(**channel_fields(**overrides))
        db.add(channel)
        await db.commit()
        return channel


def app_for(sessions, admin_id=7):
    actor = SimpleNamespace(admin_user_id=admin_id)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, channel_validation_exception_handler)

    async def bind(request: Request):
        request.state.admin_actor = actor

    async def db_dependency():
        async with sessions() as db:
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_admin_access] = lambda: actor
    app.include_router(channel_oauth.router, prefix="/api/admin")
    app.include_router(channels.router, prefix="/api/admin", dependencies=[Depends(bind)])
    return app


def callback_url(state, code="auth-code-secret"):
    from urllib.parse import urlencode
    return oauth.REDIRECT_URI + "?" + urlencode({"code": code, "state": state})


def authorization_state(response):
    from urllib.parse import parse_qs, urlsplit
    return parse_qs(urlsplit(response.authorization_url).query)["state"][0]


def test_browser_flow_parse_complete_create_consumes_once_and_keeps_credentials_private(tmp_path, monkeypatch):
    async def exercise():
        from urllib.parse import parse_qs, urlsplit
        import hashlib
        seen = []
        async def upstream(request):
            seen.append(request)
            assert str(request.url) == oauth.TOKEN_URL
            body = parse_qs(request.content.decode())
            assert body == {"grant_type": ["authorization_code"], "client_id": [oauth.CLIENT_ID],
                "code": ["auth-code-secret"], "code_verifier": [verifier], "redirect_uri": [oauth.REDIRECT_URI]}
            return Response(200, json=tokens())
        monkeypatch.setattr(channel_oauth.httpx, "AsyncClient", lambda **kw: AsyncClient(transport=MockTransport(upstream), **kw))
        async with database(tmp_path, monkeypatch) as sessions:
            app = app_for(sessions)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                start = await client.post("/api/admin/channels/oauth/sessions", json={})
                assert start.status_code == 200 and seen == []
                started = start.json()
                assert started["status"] == "pending" and started["redirect_uri"] == oauth.REDIRECT_URI
                assert not ({"user_code", "interval", "verification_uri", "pkce_verifier", "oauth_state"} & started.keys())
                session_id = started["session_id"]
                parameters = parse_qs(urlsplit(started["authorization_url"]).query)
                async with sessions() as db:
                    session = await db.get(ChannelOAuthSession, session_id)
                    verifier = session.pkce_verifier
                    assert len(verifier) == 43 and "=" not in verifier
                    assert session.oauth_state == parameters["state"][0]
                    assert session.device_auth_id is None and session.user_code is None
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                assert parameters == {"response_type": ["code"], "client_id": [oauth.CLIENT_ID],
                    "redirect_uri": [oauth.REDIRECT_URI], "scope": [oauth.SCOPE], "state": [session.oauth_state],
                    "code_challenge": [challenge], "code_challenge_method": ["S256"],
                    "id_token_add_organizations": ["true"], "codex_cli_simplified_flow": ["true"], "originator": ["rotor"]}
                callback = callback_url(parameters["state"][0])
                parsed = await client.post(f"/api/admin/channels/oauth/sessions/{session_id}/parse", json={"callback_url": callback})
                assert parsed.json() == {"code": "auth-code-secret", "state": parameters["state"][0], "redirect_uri": oauth.REDIRECT_URI}
                assert seen == []
                authorized = await client.post(f"/api/admin/channels/oauth/sessions/{session_id}/complete", json={"callback_url": callback})
                assert authorized.status_code == 200 and authorized.json()["status"] == "authorized"
                assert authorized.json()["authorization_url"] is None and len(seen) == 1
                replay = await client.post(f"/api/admin/channels/oauth/sessions/{session_id}/complete", json={"callback_url": callback})
                assert replay.status_code == 409 and len(seen) == 1
                payload = dict(name="created", type="openai_codex", auth_type="oauth", protocol="openai_responses",
                               base_url=oauth.CODEX_BASE_URL, models=["gpt-codex"], oauth_session_id=session_id)
                created = await client.post("/api/admin/channels", json=payload)
                assert created.status_code == 201, created.text
                assert created.json()["auth_status"] == "connected"
                renamed = await client.put(f"/api/admin/channels/{created.json()['id']}", json={"name": "renamed"})
                assert renamed.status_code == 200 and renamed.json()["extra"]["auth_type"] == "bearer"
                listed = await client.get("/api/admin/channels")
                detail = await client.get(f"/api/admin/channels/{created.json()['id']}")
                repeated = await client.post("/api/admin/channels", json={**payload, "name": "again"})
                assert repeated.status_code == 400
                for response in (start, parsed, authorized, replay, created, listed, detail, repeated):
                    for secret in (verifier, "refresh-secret", jwt()):
                        assert secret not in response.text
                assert "auth-code-secret" not in authorized.text
                async with sessions() as db:
                    saved = await db.get(Channel, created.json()["id"])
                    session = await db.get(ChannelOAuthSession, session_id)
                    assert saved.key == "" and saved.oauth_refresh_token == "refresh-secret"
                    assert session.status == "consumed"
                    assert all(getattr(session, name) is None for name in oauth._SESSION_SECRETS)
                removed_poll = await client.post(f"/api/admin/channels/oauth/sessions/{session_id}/poll")
                assert removed_poll.status_code == 404
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", [
    "auth-code-secret", "auth-code-secret#{state}", "?code=auth-code-secret&state={state}",
    "https://localhost:1455/auth/callback?code=auth-code-secret&state={state}",
    "http://127.0.0.1:1455/auth/callback?code=auth-code-secret&state={state}",
    "http://localhost:1456/auth/callback?code=auth-code-secret&state={state}",
    "http://localhost:01455/auth/callback?code=auth-code-secret&state={state}",
    "http://user@localhost:1455/auth/callback?code=auth-code-secret&state={state}",
    "http://localhost:1455/auth/callback/extra?code=auth-code-secret&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}#extra",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}#",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state=wrong-state-secret",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state=%E4%B8%AD",
    "http://localhost:1455/auth/callback?code=&state={state}",
    "http://localhost:1455/auth/callback?code=%20&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret",
    "http://localhost:1455/auth/callback?state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state=",
    "http://localhost:1455/auth/callback?code=auth-code-secret&code=again&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&co%64e=again&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}&foo=one&foo=two",
    "http://localhost:1455/auth/callback?error=access_denied&code=auth-code-secret&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}&error=",
    "http://localhost:1455/auth/callback?code=%FF&state={state}",
    "http://localhost:1455/auth/callback?code=%ZZ&state={state}",
    "http://localhost:1455/auth/callback?code=%&state={state}",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}&broken",
    "http://localhost:1455/auth/callback?code=auth-code-secret&state={state}\t&foo=bar",
])
def test_callback_parse_and_complete_reject_invalid_urls_without_network(tmp_path, monkeypatch, bad):
    async def exercise():
        async def no_network(request):
            raise AssertionError("Invalid callbacks must never contact the token endpoint")
        monkeypatch.setattr(channel_oauth.httpx, "AsyncClient", lambda **kw: AsyncClient(transport=MockTransport(no_network), **kw))
        async with database(tmp_path, monkeypatch) as sessions:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
            callback = bad.format(state=authorization_state(started))
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                for endpoint in ("parse", "complete"):
                    response = await client.post(f"/api/admin/channels/oauth/sessions/{started.session_id}/{endpoint}",
                        content=json.dumps({"callback_url": callback}, ensure_ascii=True), headers={"content-type": "application/json"})
                    assert response.status_code == 400, response.text
                    assert "auth-code-secret" not in response.text and "wrong-state-secret" not in response.text
            async with sessions() as db:
                saved = await db.get(ChannelOAuthSession, started.session_id)
                assert saved.status == "pending" and saved.pkce_verifier is not None
    asyncio.run(exercise())


def test_callback_owner_expiry_preview_and_complete_revalidation(tmp_path, monkeypatch):
    async def exercise():
        now = [1000.0]
        monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
        seen = []
        async def upstream(request):
            seen.append(request)
            return Response(200, json=tokens())
        async with database(tmp_path, monkeypatch) as sessions, AsyncClient(transport=MockTransport(upstream)) as client:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
                callback = callback_url(authorization_state(started))
                with pytest.raises(oauth.OAuthError) as owner:
                    await oauth.parse_callback(db, started.session_id, 8, callback)
                assert owner.value.status_code == 404
                preview = await oauth.parse_callback(db, started.session_id, 7, callback)
                assert preview.code == "auth-code-secret" and seen == []
                with pytest.raises(oauth.OAuthError) as changed:
                    await oauth.complete_session(db, client, started.session_id, 7, callback_url("wrong-state-secret"))
                assert changed.value.status_code == 400 and seen == []
                # A valid preview never extends the session deadline.
                now[0] = 1901
                for operation in (oauth.parse_callback(db, started.session_id, 7, callback),
                                  oauth.complete_session(db, client, started.session_id, 7, callback)):
                    with pytest.raises(oauth.OAuthError) as expired:
                        await operation
                    assert expired.value.status_code == 400
                assert seen == []
                saved = await db.get(ChannelOAuthSession, started.session_id)
                assert saved.status == "expired" and all(getattr(saved, name) is None for name in oauth._SESSION_SECRETS)
    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [{}, {"callback_url": None}, {"callback_url": ["auth-code-secret"]},
    {"callback_url": "auth-code-secret" * 600},
    {"callback_url": "http://localhost:1455/auth/callback?code=auth-code-secret&state=" + chr(0xD800)}])
def test_callback_schema_validation_never_echoes_callback_input(tmp_path, monkeypatch, payload):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                for endpoint in ("parse", "complete"):
                    response = await client.post(f"/api/admin/channels/oauth/sessions/{started.session_id}/{endpoint}",
                        content=json.dumps(payload, ensure_ascii=True), headers={"content-type": "application/json"})
                    assert response.status_code == 422
                    assert "auth-code-secret" not in response.text and "input" not in response.text and "ctx" not in response.text
    asyncio.run(exercise())


@pytest.mark.parametrize("expire", [False, True])
def test_cancel_and_expiry_during_exchange_prevent_late_authorization(tmp_path, monkeypatch, expire):
    async def exercise():
        now = [1000.0]
        monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
        exchanging = asyncio.Event()
        resume = asyncio.Event()
        async def upstream(request):
            exchanging.set()
            await resume.wait()
            return Response(200, json=tokens())
        async with database(tmp_path, monkeypatch) as sessions, AsyncClient(transport=MockTransport(upstream)) as client:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
                callback = callback_url(authorization_state(started))
            async with sessions() as completing_db:
                pending = asyncio.create_task(oauth.complete_session(completing_db, client, started.session_id, 7, callback))
                await exchanging.wait()
                if expire:
                    now[0] += 901
                else:
                    async with sessions() as db:
                        cancelled = await oauth.cancel_session(db, started.session_id, 7)
                        assert cancelled.status == "cancelled"
                resume.set()
                result = await pending
                assert result.status == ("expired" if expire else "cancelled")
                async with sessions() as db:
                    saved = await db.get(ChannelOAuthSession, started.session_id)
                    assert all(getattr(saved, name) is None for name in oauth._SESSION_SECRETS)
    asyncio.run(exercise())


def test_relogin_is_bound_to_administrator_channel_and_transaction(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            other = await seed(sessions, name="other")
            session_id = "authorized-session"
            async with sessions() as db:
                db.add(ChannelOAuthSession(id=session_id, admin_user_id=7, channel_id=channel.id,
                       status="authorized", interval=5, expires_at=time.time() + 900, next_poll_at=0,
                       access_token=jwt("new-account"), refresh_token="new-refresh", token_expires_at=time.time() + 3600,
                       account_id="new-account"))
                await db.commit()
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                wrong_channel = await client.put(f"/api/admin/channels/{other.id}", json={"oauth_session_id": session_id})
                assert wrong_channel.status_code == 400
                duplicate = await client.put(f"/api/admin/channels/{channel.id}", json={"name": "other", "oauth_session_id": session_id})
                assert duplicate.status_code == 400
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions, 8)), base_url="http://test") as client:
                wrong_admin = await client.put(f"/api/admin/channels/{channel.id}", json={"oauth_session_id": session_id})
                assert wrong_admin.status_code == 400
            # A downstream transaction failure leaves an authorized session reusable.
            async with sessions() as db:
                await oauth.consume_session(db, session_id, admin_user_id=7, channel_id=channel.id)
                await db.rollback()
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                updated = await client.put(f"/api/admin/channels/{channel.id}", json={"oauth_session_id": session_id})
                assert updated.status_code == 200, updated.text
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_token == "new-refresh" and saved.oauth_account_id == "new-account"
                assert (await db.get(ChannelOAuthSession, session_id)).status == "consumed"

    asyncio.run(exercise())


def test_complete_rejects_concurrency_and_replay_after_lease_expiry(tmp_path, monkeypatch):
    async def exercise():
        now = [1000.0]
        monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
        entered = asyncio.Event()
        resume = asyncio.Event()
        seen = []
        async def upstream(request):
            seen.append(request)
            entered.set()
            await resume.wait()
            return Response(200, json=tokens())
        async with database(tmp_path, monkeypatch) as sessions, AsyncClient(transport=MockTransport(upstream)) as client:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
                callback = callback_url(authorization_state(started))
            async with sessions() as first_db:
                completing = asyncio.create_task(oauth.complete_session(first_db, client, started.session_id, 7, callback))
                await entered.wait()
                async with sessions() as db:
                    busy = await db.get(ChannelOAuthSession, started.session_id)
                    assert oauth.session_response(busy).status == "exchanging"
                    assert busy.pkce_verifier is None and busy.oauth_state is None
                # Passing the lease deadline cannot make the same code reusable.
                now[0] += oauth.LOCK_LIFETIME + 1
                async with sessions() as db:
                    with pytest.raises(oauth.OAuthError) as concurrent:
                        await oauth.complete_session(db, client, started.session_id, 7, callback)
                    assert concurrent.value.status_code == 409 and len(seen) == 1
                resume.set()
                assert (await completing).status == "authorized"
                async with sessions() as db:
                    with pytest.raises(oauth.OAuthError) as replay:
                        await oauth.complete_session(db, client, started.session_id, 7, callback)
                    assert replay.value.status_code == 409 and len(seen) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["claim", "network"])
def test_cancelled_completion_releases_lease_and_cannot_replay(tmp_path, monkeypatch, stage):
    async def exercise():
        entered = asyncio.Event()
        seen = []
        async def upstream(request):
            seen.append(request)
            entered.set()
            await asyncio.Event().wait()
        async with database(tmp_path, monkeypatch) as sessions, AsyncClient(transport=MockTransport(upstream)) as client:
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
                callback = callback_url(authorization_state(started))
            class CancelledReadSession(AsyncSession):
                async def refresh(self, instance, *args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
            selected = sessions if stage == "network" else async_sessionmaker(
                sessions.kw["bind"], class_=CancelledReadSession, expire_on_commit=False)
            async with selected() as db:
                completing = asyncio.create_task(oauth.complete_session(db, client, started.session_id, 7, callback))
                await entered.wait()
                completing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await completing
            async with sessions() as db:
                saved = await db.get(ChannelOAuthSession, started.session_id)
                assert saved.status == "failed" and saved.poll_owner is None and saved.poll_lock_until is None
                assert all(getattr(saved, name) is None for name in oauth._SESSION_SECRETS)
                with pytest.raises(oauth.OAuthError) as replay:
                    await oauth.complete_session(db, client, started.session_id, 7, callback)
                assert replay.value.status_code == 409
                assert len(seen) == (1 if stage == "network" else 0)
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["upstream", "invalid_token", "timeout"])
def test_completion_failure_is_safe_terminal_and_single_use(tmp_path, monkeypatch, mode):
    async def exercise():
        seen = []
        async def upstream(request):
            seen.append(request)
            if mode == "upstream":
                return Response(400, text="auth-code-secret refresh-secret verifier-secret")
            if mode == "invalid_token":
                return Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": False})
            await asyncio.Event().wait()
        async with database(tmp_path, monkeypatch) as sessions, AsyncClient(transport=MockTransport(upstream)) as client, sessions() as db:
            started = await oauth.start_session(db, admin_user_id=7, channel_id=None)
            callback = callback_url(authorization_state(started))
            if mode == "timeout":
                monkeypatch.setattr(oauth, "HTTP_TIMEOUT", 0.03)
            failed = await oauth.complete_session(db, client, started.session_id, 7, callback)
            assert failed.status == "failed" and failed.authorization_url is None
            for secret in ("auth-code-secret", "refresh-secret", "verifier-secret", "access-secret"):
                assert secret not in failed.model_dump_json()
            saved = await db.get(ChannelOAuthSession, started.session_id)
            assert all(getattr(saved, name) is None for name in oauth._SESSION_SECRETS)
            with pytest.raises(oauth.OAuthError):
                await oauth.complete_session(db, client, started.session_id, 7, callback)
            assert len(seen) == 1
    asyncio.run(exercise())


def test_local_session_creation_checks_channel_and_pending_cancel_clears_pkce(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions, sessions() as db:
            with pytest.raises(oauth.OAuthError) as missing:
                await oauth.start_session(db, admin_user_id=7, channel_id=999)
            assert missing.value.status_code == 404
            assert list((await db.scalars(select(ChannelOAuthSession))).all()) == []
            first = await oauth.start_session(db, admin_user_id=7, channel_id=None)
            second = await oauth.start_session(db, admin_user_id=7, channel_id=None)
            assert authorization_state(first) != authorization_state(second)
            with pytest.raises(oauth.OAuthError) as wrong_owner:
                await oauth.cancel_session(db, first.session_id, 8)
            assert wrong_owner.value.status_code == 404
            cancelled = await oauth.cancel_session(db, first.session_id, 7)
            assert cancelled.status == "cancelled" and cancelled.authorization_url is None
            saved = await db.get(ChannelOAuthSession, first.session_id)
            assert all(getattr(saved, name) is None for name in oauth._SESSION_SECRETS)
    asyncio.run(exercise())


def test_browser_relogin_changes_saved_credentials_only_after_channel_update(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with sessions() as db:
                started = await oauth.start_session(db, admin_user_id=7, channel_id=channel.id)
                callback = callback_url(authorization_state(started))
                async with AsyncClient(transport=MockTransport(lambda request: Response(200, json=tokens("new-account", "new-refresh")))) as client:
                    authorized = await oauth.complete_session(db, client, started.session_id, 7, callback)
                assert authorized.status == "authorized"
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_account_id == "account-1" and saved.oauth_refresh_token == "refresh-secret"
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.put(f"/api/admin/channels/{channel.id}", json={"oauth_session_id": started.session_id})
                assert response.status_code == 200, response.text
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_account_id == "new-account" and saved.oauth_refresh_token == "new-refresh"
    asyncio.run(exercise())


def test_login_routes_require_admin_authentication_before_network(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            app = FastAPI()
            app.include_router(channel_oauth.router, prefix="/api/admin")
            async def override_db():
                async with sessions() as db:
                    yield db
            app.dependency_overrides[get_db] = override_db
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for method, path, body in [("POST", "", {}), ("POST", "/unknown/parse", {"callback_url": "unused"}), ("POST", "/unknown/complete", {"callback_url": "unused"}), ("POST", "/unknown/probe-models", None), ("DELETE", "/unknown", None)]:
                    response = await client.request(method, "/api/admin/channels/oauth/sessions" + path, json=body)
                    assert response.status_code == 401
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", [None, {}, [], {"access_token": 123, "refresh_token": "secret", "expires_in": 3600},
    {"access_token": " ", "refresh_token": "secret", "expires_in": 3600},
    {"access_token": jwt(), "refresh_token": [], "expires_in": 3600},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": True},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": "3600"},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": 0},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": -1},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": math.inf},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": math.nan},
    {"access_token": jwt(), "refresh_token": "secret", "expires_in": 10 ** 400},
    {"access_token": jwt(""), "refresh_token": "secret", "expires_in": 3600},
    {"access_token": jwt([]), "refresh_token": "secret", "expires_in": 3600}])
def test_invalid_credentials_fail_without_echoing_payload(bad):
    with pytest.raises(oauth.OAuthError) as raised:
        oauth._credentials(bad)
    assert raised.value.response.status_code == 401
    assert "secret" not in str(raised.value)
    assert "secret" not in raised.value.response.text
    assert normalize_upstream_error(raised.value).fallback_allowed


def test_base64url_account_and_public_credential_has_no_refresh():
    token = jwt("tenant-\uffff")
    assert "_" in token or "-" in token
    assert oauth.extract_account_id(token) == "tenant-\uffff"
    credential = oauth.OAuthCredentials(token, "tenant-\uffff", 123)
    assert not hasattr(credential, "refresh_token")
    assert token not in repr(credential)


def test_refresh_serializes_concurrent_readers_and_rereads_stale_channel(tmp_path, monkeypatch):
    async def exercise():
        count = []

        async def upstream(request):
            count.append(request)
            assert b"refresh_token=refresh-secret" in request.content
            await asyncio.sleep(0.1)
            return Response(200, json=tokens("rotated", "rotated-refresh"))

        async with database(tmp_path, monkeypatch) as sessions:
            stale = await seed(sessions, oauth_expires_at=time.time() + 300)
            async with AsyncClient(transport=MockTransport(upstream)) as client:
                first, second = await asyncio.gather(oauth.resolve_oauth_credentials(stale, client), oauth.resolve_oauth_credentials(stale, client))
                third = await oauth.resolve_oauth_credentials(stale, client)
            assert len(count) == 1
            assert first.account_id == second.account_id == third.account_id == "rotated"
            async with sessions() as db:
                saved = await db.get(Channel, stale.id)
                assert saved.oauth_refresh_token == "rotated-refresh"
                assert saved.oauth_refresh_owner is None
                assert saved.oauth_status == "connected"

    asyncio.run(exercise())


def test_relogin_after_claim_before_locked_read_does_not_rotate_new_token(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            seen = []
            class RacingSession(AsyncSession):
                async def refresh(self, instance, *args, **kwargs):
                    if isinstance(instance, Channel) and not seen:
                        seen.append(instance.id)
                        async with sessions() as other_db:
                            await other_db.execute(update(Channel).where(Channel.id == instance.id).values(
                                **oauth.credential_values(oauth._credentials(tokens("new-account", "new-refresh")))))
                            await other_db.commit()
                    return await super().refresh(instance, *args, **kwargs)
            racing = async_sessionmaker(sessions.kw["bind"], class_=RacingSession, expire_on_commit=False)
            monkeypatch.setattr(oauth, "async_session_maker", racing)
            async def reject_network(request):
                raise AssertionError("A saved fresh relogin must not be rotated by the stale refresh claimant")
            async with AsyncClient(transport=MockTransport(reject_network)) as client:
                credential = await oauth.resolve_oauth_credentials(channel, client)
            assert credential.account_id == "new-account" and seen == [channel.id]
    asyncio.run(exercise())


def test_cancellation_after_refresh_claim_releases_lease_before_network(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            entered = asyncio.Event()
            class CancelledReadSession(AsyncSession):
                async def refresh(self, instance, *args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
            interrupted = async_sessionmaker(sessions.kw["bind"], class_=CancelledReadSession, expire_on_commit=False)
            monkeypatch.setattr(oauth, "async_session_maker", interrupted)
            async def no_network(request):
                raise AssertionError("Cancellation happened before the token request")
            async with AsyncClient(transport=MockTransport(no_network)) as client:
                resolving = asyncio.create_task(oauth.resolve_oauth_credentials(channel, client))
                await entered.wait()
                resolving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await resolving
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_owner is None and saved.oauth_refresh_lock_until is None
    asyncio.run(exercise())


def test_fresh_credential_resolution_performs_no_lease_writes(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            statements = []
            @event.listens_for(sessions.kw["bind"].sync_engine, "before_cursor_execute")
            def observe(connection, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            async def no_network(request):
                raise AssertionError("Fresh credentials do not require a token exchange")
            async with AsyncClient(transport=MockTransport(no_network)) as client:
                result = await oauth.resolve_oauth_credentials(channel, client)
            assert result.account_id == "account-1"
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["success", "failure", "api_key", "delete", "cancel"])
def test_refresh_race_does_not_overwrite_relogin_or_authentication_change(tmp_path, monkeypatch, outcome):
    async def exercise():
        entered = asyncio.Event()
        resume = asyncio.Event()

        async def upstream(request):
            entered.set()
            await resume.wait()
            if outcome == "failure":
                return Response(400, text="invalid_grant reflected refresh-secret")
            return Response(200, json=tokens("late-account", "late-refresh"))

        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=MockTransport(upstream)) as client:
                refreshing = asyncio.create_task(oauth.resolve_oauth_credentials(channel, client))
                await entered.wait()
                async with sessions() as db:
                    saved = await db.get(Channel, channel.id)
                    if outcome in {"success", "failure"}:
                        values = oauth.credential_values(oauth._credentials(tokens("new-account", "new-refresh")))
                        await db.execute(update(Channel).where(Channel.id == channel.id).values(**values))
                    elif outcome == "api_key":
                        await db.execute(update(Channel).where(Channel.id == channel.id).values(
                            auth_type="api_key", type="openai", key="new-key", **oauth.clear_credential_values()))
                    elif outcome == "delete":
                        await db.delete(saved)
                    await db.commit()
                if outcome == "cancel":
                    refreshing.cancel()
                resume.set()
                if outcome in {"success", "failure"}:
                    result = await refreshing
                    assert result.account_id == "new-account"
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await refreshing
                else:
                    with pytest.raises(oauth.OAuthError):
                        await refreshing
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                if outcome == "delete":
                    assert saved is None
                else:
                    assert saved.oauth_refresh_owner is None
                    assert saved.oauth_refresh_token != "late-refresh"
                    if outcome in {"success", "failure"}:
                        assert saved.oauth_status == "connected" and saved.oauth_error is None

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["relogin", "api_key"])
def test_saved_channel_update_invalidates_inflight_refresh_lease(tmp_path, monkeypatch, change):
    async def exercise():
        entered = asyncio.Event()
        resume = asyncio.Event()
        async def upstream(request):
            entered.set()
            await resume.wait()
            return Response(200, json=tokens("late-account", "late-refresh"))
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with sessions() as db:
                db.add(ChannelOAuthSession(id="relogin-session", admin_user_id=7, channel_id=channel.id,
                       status="authorized", interval=5, expires_at=time.time() + 900, next_poll_at=0,
                       access_token=jwt("new-account"), refresh_token="new-refresh", token_expires_at=time.time() + 3600,
                       account_id="new-account"))
                await db.commit()
            # Pause the update after reading the old channel, before it writes.
            update_read = asyncio.Event()
            resume_update = asyncio.Event()
            original_validate = channels._validate_channel_auth
            def validation_barrier(**kwargs):
                original_validate(**kwargs)
                update_read.set()
            monkeypatch.setattr(channels, "_validate_channel_auth", validation_barrier)
            original_consume = channels.consume_session
            async def consume_after_claim(*args, **kwargs):
                await resume_update.wait()
                return await original_consume(*args, **kwargs)
            monkeypatch.setattr(channels, "consume_session", consume_after_claim)
            # For API key switching, pause the authentication validator's DB
            # dependency before the channel query returns to the route.
            app = app_for(sessions)
            if change == "api_key":
                class ReadBarrierSession(AsyncSession):
                    async def execute(self, statement, *args, **kwargs):
                        result = await super().execute(statement, *args, **kwargs)
                        if isinstance(statement, Select) and not update_read.is_set():
                            update_read.set()
                            await resume_update.wait()
                        return result
                barriers = async_sessionmaker(sessions.kw["bind"], class_=ReadBarrierSession, expire_on_commit=False)
                async def route_db():
                    async with barriers() as db:
                        yield db
                app.dependency_overrides[get_db] = route_db
            async with AsyncClient(transport=MockTransport(upstream)) as upstream_client, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
                payload = {"oauth_session_id": "relogin-session"} if change == "relogin" else {
                    "auth_type": "api_key", "type": "openai", "protocol": "openai", "base_url": "https://example.invalid", "key": "new-api-key"}
                saving = asyncio.create_task(admin.put(f"/api/admin/channels/{channel.id}", json=payload))
                await update_read.wait()
                refreshing = asyncio.create_task(oauth.resolve_oauth_credentials(channel, upstream_client))
                await entered.wait()
                resume_update.set()
                response = await saving
                assert response.status_code == 200, response.text
                resume.set()
                if change == "relogin":
                    assert (await refreshing).account_id == "new-account"
                else:
                    with pytest.raises(oauth.OAuthError):
                        await refreshing
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_owner is None
                assert saved.oauth_refresh_token == ("new-refresh" if change == "relogin" else None)
    asyncio.run(exercise())


def test_refresh_failure_status_and_timeout_are_safe_and_retryable(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async def invalid(request):
                return Response(400, json={"error": "invalid_grant", "refresh_token": "refresh-secret", "access_token": jwt()})
            async with AsyncClient(transport=MockTransport(invalid)) as client:
                with pytest.raises(oauth.OAuthError) as error:
                    await oauth.resolve_oauth_credentials(channel, client)
                assert error.value.status_code == 401
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_status == "reauth_required" and "refresh-secret" not in saved.oauth_error
                assert saved.oauth_refresh_token == "refresh-secret"
                assert saved.oauth_refresh_owner is None
            channel = await seed(sessions, name="timeout-channel", oauth_expires_at=time.time() - 1)
            async def hanging(request):
                await asyncio.sleep(10)
            monkeypatch.setattr(oauth, "HTTP_TIMEOUT", 0.03)
            async with AsyncClient(transport=MockTransport(hanging)) as client:
                started = time.monotonic()
                with pytest.raises(oauth.OAuthError) as error:
                    await oauth.resolve_oauth_credentials(channel, client)
                assert time.monotonic() - started < 1
                assert error.value.status_code == 503
                assert normalize_upstream_error(error.value).retry_same_channel
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).oauth_refresh_owner is None

    asyncio.run(exercise())


def test_unsaved_oauth_catalog_requires_login_and_test_only_validates_credentials(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async def no_network(**kwargs):
                raise AssertionError("OAuth checks must not probe a model catalog")
            monkeypatch.setattr(channels, "_fetch_model_list", no_network)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                unsaved = await client.post("/api/admin/channels/probe-models", json={"type": "openai_codex", "auth_type": "oauth", "base_url": oauth.CODEX_BASE_URL})
                assert unsaved.status_code == 400
                checked = await client.post(f"/api/admin/channels/{channel.id}/test")
                assert checked.status_code == 200 and checked.json()["ok"]
                assert checked.json()["check_type"] == "oauth"

    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [{"auth_type": None}, {"key": None}, {"key": ""}, {"extra": {"refresh_token": "refresh-secret"}},
    {"extra": {"extension": {"refresh_token": "refresh-secret"}}}])
def test_auth_validation_errors_do_not_echo_secrets(tmp_path, monkeypatch, payload):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                response = await client.put(f"/api/admin/channels/{channel.id}", json=payload)
                assert response.status_code == 422
                assert "refresh-secret" not in response.text
                assert "input" not in response.text and "ctx" not in response.text

    asyncio.run(exercise())


def test_api_key_create_requires_key_and_response_omits_private_fields():
    with pytest.raises(ValueError, match="API key is required"):
        ChannelCreate(name="plain", type="openai", base_url="https://example.invalid")
    for schema in (ChannelCreate, ChannelUpdate, ChannelResponse):
        assert not ({"oauth_access_token", "oauth_refresh_token", "oauth_account_id"} & schema.model_fields.keys())


def test_refresh_is_serialized_across_processes(tmp_path, monkeypatch):
    async def setup():
        async with database(tmp_path, monkeypatch) as sessions:
            return (await seed(sessions, oauth_expires_at=time.time() - 1)).id
    channel_id = asyncio.run(setup())
    marker = tmp_path / "refreshes"
    code = '''import asyncio, os
from httpx import AsyncClient, MockTransport, Response
from rotor.models.channel import Channel
from rotor.services.chatgpt_oauth import resolve_oauth_credentials
async def upstream(request):
    with open(os.environ["REFRESH_MARKER"], "a") as output:
        output.write("refresh\\n")
    await asyncio.sleep(.3)
    return Response(200, json=__import__("json").loads(os.environ["MOCK_TOKENS"]))
async def run():
    async with AsyncClient(transport=MockTransport(upstream)) as client:
        value = await resolve_oauth_credentials(Channel(id=int(os.environ["CHANNEL_ID"])), client)
        assert value.account_id == "process-account"
asyncio.run(run())
'''
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'oauth.db'}",
                   "REFRESH_MARKER": str(marker), "MOCK_TOKENS": json.dumps(tokens("process-account", "process-refresh")),
                   "CHANNEL_ID": str(channel_id)}
    workers = [subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for worker in workers:
        output, errors = worker.communicate(timeout=20)
        assert worker.returncode == 0, output + errors
    assert marker.read_text().splitlines() == ["refresh"]


def test_sql_echo_hides_oauth_credentials(tmp_path, caplog):
    async def exercise():
        engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'echo.db'}", echo=True)
        async with engine.begin() as connection:
            await connection.run_sync(Channel.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(Channel(**channel_fields(oauth_access_token="access-secret-never-log", oauth_refresh_token="refresh-secret-never-log")))
            await db.commit()
        await engine.dispose()
    asyncio.run(exercise())
    assert "INSERT INTO channels" in caplog.text
    assert "access-secret-never-log" not in caplog.text
    assert "refresh-secret-never-log" not in caplog.text
    assert "SQL parameters hidden" in caplog.text
