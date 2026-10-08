import asyncio
import time

from httpx import ASGITransport, AsyncClient, MockTransport, ReadTimeout, Response
import pytest
from sqlalchemy import event

from rotor.models.channel import Channel
from rotor.models.channel_oauth import ChannelOAuthSession
from rotor.services import chatgpt_oauth as oauth
from tests.test_chatgpt_oauth_backend import app_for, database, jwt, seed, tokens


def model(slug, visibility="list", supported_in_api=True):
    return {"slug": slug, "visibility": visibility, "supported_in_api": supported_in_api}


async def login(sessions, **overrides):
    fields = dict(id="catalog-session", admin_user_id=7, channel_id=None, status="authorized",
                  interval=0, next_poll_at=0, expires_at=time.time() + 900,
                  access_token=jwt(), refresh_token="refresh-secret", account_id="account-1",
                  token_expires_at=time.time() + 3600)
    fields.update(overrides)
    async with sessions() as db:
        session = ChannelOAuthSession(**fields)
        db.add(session)
        await db.commit()
        return session


def mock_upstream(monkeypatch, upstream):
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))


def test_saved_oauth_catalog_refreshes_and_ignores_connection_overrides(tmp_path, monkeypatch):
    async def exercise():
        requests = []
        async def upstream(request):
            requests.append(request)
            if request.method == "POST":
                assert str(request.url) == oauth.TOKEN_URL
                assert b"refresh_token=refresh-secret" in request.content
                return Response(200, json=tokens("rotated-account", "rotated-refresh"))
            assert request.headers["Authorization"] == "Bearer " + jwt("rotated-account")
            assert request.headers["chatgpt-account-id"] == "rotated-account"
            return Response(200, json={"models": [model("zeta"), model("hidden", "hide"), model("alpha", supported_in_api=False),
                                                model("zeta"), model("none", "none")]})
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models", json={
                    "base_url": "https://untrusted.example/steal", "type": "other", "protocol": "anthropic",
                    "extra": {"models_path": "https://untrusted.example/models", "headers": {"Authorization": "attacker", "X-Draft": "leak"}},
                })
            assert response.status_code == 200
            assert response.json()["models"] == ["zeta", "hidden", "alpha", "none"] and response.json()["raw_count"] == 5
            assert set(response.json()) == {"models", "latency_ms", "raw_count"}
            assert isinstance(response.json()["latency_ms"], int)
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.models == ["gpt-codex"] and saved.extra == {} and saved.base_url == oauth.CODEX_BASE_URL
                assert saved.oauth_refresh_token == "rotated-refresh" and saved.oauth_status == "connected"
        assert len(requests) == 2
        request = requests[1]
        assert str(request.url) == oauth.CODEX_BASE_URL + "/models?client_version=0.161.0"
        assert request.method == "GET" and request.headers["originator"] == "rotor"
        assert request.headers["accept"] == "application/json" and "x-draft" not in request.headers
        assert set(request.extensions["timeout"].values()) == {15.0}
    asyncio.run(exercise())


def test_authorized_session_catalog_is_read_only_and_can_be_probed_repeatedly(tmp_path, monkeypatch):
    async def exercise():
        requests = []
        statements = []
        async def upstream(request):
            requests.append(request)
            assert request.headers["Authorization"] == "Bearer " + jwt()
            assert request.headers["chatgpt-account-id"] == "account-1"
            return Response(200, json={"models": [model("second"), model("first"), model("second")]})
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            original = await login(sessions)
            @event.listens_for(sessions.kw["bind"].sync_engine, "before_cursor_execute")
            def record(connection, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                for _ in range(2):
                    response = await admin.post(f"/api/admin/channels/oauth/sessions/{original.id}/probe-models")
                    assert response.status_code == 200 and response.json()["models"] == ["second", "first"]
                    assert response.json()["raw_count"] == 3
            async with sessions() as db:
                saved = await db.get(ChannelOAuthSession, original.id)
                assert saved.status == "authorized" and saved.access_token == original.access_token
                assert saved.refresh_token == original.refresh_token and saved.token_expires_at == original.token_expires_at
            assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert len(requests) == 2
        assert all(str(request.url) == oauth.CODEX_BASE_URL + "/models?client_version=0.161.0" for request in requests)
    asyncio.run(exercise())


@pytest.mark.parametrize("target", ["saved", "session"])
@pytest.mark.parametrize("version", ["default", "0.155.0"])
@pytest.mark.parametrize("duplicates", [False, True])
def test_catalog_version_preserves_complete_account_catalog_including_hidden_models(tmp_path, monkeypatch, target, version, duplicates):
    async def exercise():
        catalog = [
            model("gpt-6.1-sol"), model("gpt-6-astra"), model("gpt-6-sol"), model("gpt-6-luna"),
            model("gpt-reserve", "hide"), model("gpt-5.6-sol"), model("gpt-5.6-terra"),
            model("gpt-5.6-luna"), model("gpt-5.5", "hide"), model("codex-auto-review", "hide"),
        ]
        requests = []
        async def upstream(request):
            requests.append(request)
            # Recorded catalog responses differ by the requested Codex compatibility version.
            entries = catalog if request.url.params["client_version"] == "0.161.0" else catalog[1:]
            if duplicates:
                entries = [*entries, model("gpt-reserve", "none"), model("gpt-6-sol")]
            return Response(200, json={"models": entries})
        mock_upstream(monkeypatch, upstream)
        if version != "default":
            monkeypatch.setattr(oauth, "CATALOG_CLIENT_VERSION", version)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            session = await login(sessions)
            url = f"/api/admin/channels/{channel.id}/probe-models" if target == "saved" else f"/api/admin/channels/oauth/sessions/{session.id}/probe-models"
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(url)
                assert response.status_code == 200
                expected = [entry["slug"] for entry in (catalog if version == "default" else catalog[1:])]
                assert response.json()["models"] == expected
                assert response.json()["raw_count"] == len(expected) + (2 if duplicates else 0)
                assert set(response.json()) == {"models", "latency_ms", "raw_count"}
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).models == ["gpt-codex"]
                saved_session = await db.get(ChannelOAuthSession, session.id)
                assert saved_session.status == "authorized" and saved_session.refresh_token == "refresh-secret"
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == oauth.CODEX_BASE_URL + "/models?client_version=" + ("0.161.0" if version == "default" else version)
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer " + jwt()
        assert request.headers["chatgpt-account-id"] == "account-1" and request.headers["originator"] == "rotor"
        assert request.headers["accept"] == "application/json"
    asyncio.run(exercise())


@pytest.mark.parametrize("reflected", ["refresh-secret", "rotated-refresh"])
def test_saved_catalog_protects_refresh_tokens_before_and_after_rotation(tmp_path, monkeypatch, reflected):
    async def exercise():
        requests = []
        async def upstream(request):
            requests.append(request)
            if request.method == "POST":
                return Response(200, json=tokens("rotated-account", "rotated-refresh"))
            return Response(200, json={"models": [model(reflected)]})
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
                assert response.status_code == 502
                assert "refresh-secret" not in response.text and "rotated-refresh" not in response.text
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_token == "rotated-refresh" and saved.models == ["gpt-codex"]
        assert len(requests) == 2
    asyncio.run(exercise())


def test_saved_catalog_refresh_failure_is_safe_and_does_not_query_models(tmp_path, monkeypatch):
    async def exercise():
        requests = []
        async def upstream(request):
            requests.append(request)
            assert request.method == "POST" and str(request.url) == oauth.TOKEN_URL
            return Response(400, text="refresh-secret account-1 " + jwt())
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
                assert response.status_code == 400
                assert all(secret not in response.text for secret in (jwt(), "refresh-secret", "account-1"))
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).models == ["gpt-codex"]
        assert len(requests) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("fields,status", [
    ({"admin_user_id": 8}, 404), ({"status": "pending"}, 409), ({"status": "exchanging"}, 409),
    ({"status": "failed"}, 409), ({"status": "expired"}, 409), ({"status": "consumed"}, 409),
    ({"status": "cancelled"}, 409), ({"expires_at": 1}, 400), ({"token_expires_at": 1}, 400),
    ({"token_expires_at": None}, 400), ({"access_token": None}, 400), ({"account_id": None}, 400),
])
def test_session_catalog_rejects_ineligible_sessions_without_network_or_mutation(tmp_path, monkeypatch, fields, status):
    async def exercise():
        async def no_network(request):
            raise AssertionError("An ineligible login must not contact upstream")
        mock_upstream(monkeypatch, no_network)
        async with database(tmp_path, monkeypatch) as sessions:
            original = await login(sessions, **fields)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/oauth/sessions/{original.id}/probe-models")
                assert response.status_code == status
                missing = await admin.post("/api/admin/channels/oauth/sessions/missing/probe-models")
                assert missing.status_code == 404
            async with sessions() as db:
                saved = await db.get(ChannelOAuthSession, original.id)
                assert saved.status == original.status and saved.refresh_token == original.refresh_token
                assert saved.access_token == original.access_token and saved.expires_at == original.expires_at
    asyncio.run(exercise())


@pytest.mark.parametrize("target", ["saved", "session"])
@pytest.mark.parametrize("payload", [
    {}, [], {"models": None}, {"models": {}}, {"models": ["slug"]}, {"models": [{}]},
    {"models": [model(3)]}, {"models": [model("")]}, {"models": [model("has whitespace")]},
    {"models": [model("has\x00control")]}, {"models": [{"slug": "id", "supported_in_api": True}]},
    {"models": [model("id", "invalid")]}, {"models": [{"slug": "id", "visibility": "list"}]},
    {"models": [model("id", supported_in_api="true")]}, {"error": "access-secret", "models": []},
    {"models": [model("refresh-secret")]}, {"models": [model("account-1")]},
    {"models": [model(jwt())]},
])
def test_oauth_catalog_invalid_or_reflected_secret_payloads_fail_safely(tmp_path, monkeypatch, target, payload):
    async def exercise():
        async def upstream(request):
            return Response(200, json=payload)
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            session = await login(sessions)
            url = f"/api/admin/channels/{channel.id}/probe-models" if target == "saved" else f"/api/admin/channels/oauth/sessions/{session.id}/probe-models"
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(url)
                assert response.status_code == 502 and "models" not in response.json()
                assert all(secret not in response.text for secret in (jwt(), "refresh-secret", "account-1", "access-secret"))
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).models == ["gpt-codex"]
                assert (await db.get(ChannelOAuthSession, session.id)).status == "authorized"
    asyncio.run(exercise())


@pytest.mark.parametrize("target", ["saved", "session"])
@pytest.mark.parametrize("mode,status", [("empty", 200), ("401", 400), ("503", 503), ("redirect", 503),
                                         ("non-json", 502), ("transport", 503), ("deadline", 503)])
def test_oauth_catalog_empty_errors_timeout_and_redirect_are_safe(tmp_path, monkeypatch, target, mode, status):
    async def exercise():
        requests = []
        async def upstream(request):
            requests.append(request)
            if mode == "empty":
                return Response(200, json={"models": []})
            if mode == "transport":
                raise ReadTimeout("refresh-secret " + jwt())
            if mode == "deadline":
                await asyncio.Event().wait()
            if mode == "redirect":
                return Response(302, headers={"Location": "https://untrusted.example/steal"})
            return Response(200 if mode == "non-json" else int(mode), text="refresh-secret account-1 " + jwt())
        mock_upstream(monkeypatch, upstream)
        if mode == "deadline":
            monkeypatch.setattr(oauth, "HTTP_TIMEOUT", 0.03)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            session = await login(sessions)
            url = f"/api/admin/channels/{channel.id}/probe-models" if target == "saved" else f"/api/admin/channels/oauth/sessions/{session.id}/probe-models"
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(url)
                assert response.status_code == status
                assert all(secret not in response.text for secret in (jwt(), "refresh-secret", "account-1"))
                if mode == "empty":
                    assert response.json()["models"] == [] and response.json()["raw_count"] == 0
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).models == ["gpt-codex"]
                assert (await db.get(ChannelOAuthSession, session.id)).status == "authorized"
        assert len(requests) == 1 and requests[0].url.host == "chatgpt.com"
    asyncio.run(exercise())


@pytest.mark.parametrize("target,failure", [
    ("saved", "upstream_401"), ("saved", "refresh_failed"), ("session", "upstream_401"),
    ("session", "token_expired"), ("session", "token_missing"),
])
def test_catalog_chatgpt_auth_failure_does_not_invalidate_rotor_admin_login(tmp_path, monkeypatch, target, failure):
    async def exercise():
        requests = []
        async def upstream(request):
            requests.append(request)
            return Response(401, text="refresh-secret account-1 " + jwt())
        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=1 if failure == "refresh_failed" else time.time() + 3600)
            session = await login(sessions, **({"token_expires_at": 1} if failure == "token_expired" else
                                               {"access_token": None} if failure == "token_missing" else {}))
            url = f"/api/admin/channels/{channel.id}/probe-models" if target == "saved" else f"/api/admin/channels/oauth/sessions/{session.id}/probe-models"
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(url)
                assert response.status_code == 400
                assert "sign in again" in response.json()["detail"]
                assert all(secret not in response.text for secret in (jwt(), "refresh-secret", "account-1"))
        assert len(requests) == (0 if failure in {"token_expired", "token_missing"} else 1)
    asyncio.run(exercise())
