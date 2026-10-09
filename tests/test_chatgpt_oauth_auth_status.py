import asyncio
import math
import time

from httpx import ASGITransport, AsyncClient, MockTransport, ReadTimeout, Response
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Update

from rotor.core.exceptions import normalize_upstream_error
from rotor.models.channel import Channel
from rotor.services import chatgpt_oauth as oauth
from tests.test_chatgpt_oauth_backend import app_for, channel_fields, database, jwt, seed, tokens


@pytest.mark.parametrize("overrides,expected", [
    ({"auth_type": "api_key"}, "api_key"),
    ({"oauth_status": "connected"}, "connected"),
    ({"oauth_status": None}, "connected"),
    ({"oauth_status": "error", "oauth_error": "invalid_grant"}, "temporary_error"),
    ({"oauth_status": "temporary_error"}, "temporary_error"),
    ({"oauth_status": "reauth_required"}, "reauth_required"),
    ({"oauth_expires_at": 1.0}, "connected"),
    ({"oauth_access_token": None}, "unauthed"),
    ({"oauth_access_token": " "}, "unauthed"),
    ({"oauth_refresh_token": None}, "unauthed"),
    ({"oauth_account_id": ""}, "unauthed"),
    ({"oauth_expires_at": None}, "unauthed"),
    ({"oauth_expires_at": 0}, "unauthed"),
    ({"oauth_expires_at": math.inf}, "unauthed"),
    ({"oauth_expires_at": math.nan}, "unauthed"),
    ({"oauth_expires_at": True}, "unauthed"),
])
def test_auth_status_depends_on_complete_credentials_not_access_token_expiry(overrides, expected):
    assert Channel(**channel_fields(**overrides)).auth_status == expected


@pytest.mark.parametrize("status,expected", [
    ("connected", "connected"),
    ("error", "temporary_error"),
    ("temporary_error", "temporary_error"),
    ("reauth_required", "reauth_required"),
])
def test_channel_list_and_detail_expose_safe_authorization_state(tmp_path, monkeypatch, status, expected):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_status=status, oauth_error="Safe refresh failure")
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                listed = await client.get("/api/admin/channels")
                detail = await client.get(f"/api/admin/channels/{channel.id}")
            assert listed.status_code == detail.status_code == 200
            for payload in (listed.json()[0], detail.json()):
                assert payload["auth_status"] == expected
                assert payload["auth_expires_at"] == channel.oauth_expires_at
                assert payload["auth_error"] == "Safe refresh failure"
                assert not {"oauth_access_token", "oauth_refresh_token", "oauth_account_id"} & payload.keys()
                assert "refresh-secret" not in str(payload) and jwt() not in str(payload)
    asyncio.run(exercise())


@pytest.mark.parametrize("upstream_status,payload,text,expected,status_code", [
    (400, {"error": "invalid_grant"}, None, "reauth_required", 401),
    (400, {"error": {"code": "invalid_grant"}}, None, "reauth_required", 401),
    (401, {}, None, "reauth_required", 401),
    (401, {"error": "invalid_client"}, None, "temporary_error", 401),
    (403, {"error": "invalid_grant"}, None, "reauth_required", 401),
    (403, {"error": "permission_denied"}, None, "temporary_error", 401),
    (403, None, "<html>Forbidden</html>", "temporary_error", 401),
    (400, {"error": "invalid_request"}, None, "temporary_error", 401),
    (400, None, "invalid_grant reflected refresh-secret", "temporary_error", 401),
    (429, {"error": "invalid_grant"}, None, "temporary_error", 503),
    (503, {"error": "invalid_grant"}, None, "temporary_error", 503),
    (200, None, "invalid JSON refresh-secret", "temporary_error", 401),
    (200, {}, None, "temporary_error", 401),
    (200, {"access_token": "invalid", "refresh_token": "new-secret", "expires_in": 3600}, None, "temporary_error", 401),
])
def test_refresh_failure_is_classified_without_discarding_or_echoing_credentials(
    tmp_path, monkeypatch, upstream_status, payload, text, expected, status_code,
):
    async def exercise():
        async def upstream(request):
            return Response(upstream_status, json=payload) if text is None else Response(upstream_status, text=text)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=MockTransport(upstream)) as client:
                with pytest.raises(oauth.OAuthError) as caught:
                    await oauth.resolve_oauth_credentials(channel, client)
            assert caught.value.auth_status == expected
            assert caught.value.status_code == status_code
            assert normalize_upstream_error(caught.value).fallback_allowed
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_status == saved.auth_status == expected
                assert saved.oauth_refresh_token == "refresh-secret" and saved.oauth_access_token == jwt()
                assert saved.oauth_account_id == "account-1" and saved.oauth_expires_at == channel.oauth_expires_at
                assert saved.oauth_refresh_owner is None and saved.oauth_refresh_lock_until is None
                for secret in ("refresh-secret", "new-secret", jwt()):
                    assert secret not in saved.oauth_error
                    assert secret not in str(caught.value) and secret not in caught.value.response.text
                if expected == "temporary_error":
                    assert "sign in again" not in saved.oauth_error
    asyncio.run(exercise())


def test_refresh_timeout_marks_temporary_failure_and_next_success_clears_it(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async def timeout(request):
                raise ReadTimeout("refresh-secret must not be echoed", request=request)
            async with AsyncClient(transport=MockTransport(timeout)) as client:
                with pytest.raises(oauth.OAuthError) as caught:
                    await oauth.resolve_oauth_credentials(channel, client)
            assert caught.value.status_code == 503 and caught.value.auth_status == "temporary_error"
            async with sessions() as db:
                failed = await db.get(Channel, channel.id)
                assert failed.auth_status == "temporary_error" and failed.oauth_error
            async with AsyncClient(transport=MockTransport(lambda request: Response(200, json=tokens("rotated", "new-refresh")))) as client:
                assert (await oauth.resolve_oauth_credentials(channel, client)).account_id == "rotated"
            async with sessions() as db:
                restored = await db.get(Channel, channel.id)
                assert restored.auth_status == restored.oauth_status == "connected"
                assert restored.oauth_error is None and restored.oauth_refresh_token == "new-refresh"
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["timeout", "invalid_grant"])
def test_late_refresh_failure_does_not_overwrite_new_login(tmp_path, monkeypatch, failure):
    async def exercise():
        entered, resume = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            entered.set()
            await resume.wait()
            if failure == "timeout":
                raise ReadTimeout("old refresh failed", request=request)
            return Response(400, json={"error": "invalid_grant"})
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            async with AsyncClient(transport=MockTransport(upstream)) as client:
                refreshing = asyncio.create_task(oauth.resolve_oauth_credentials(channel, client))
                await asyncio.wait_for(entered.wait(), timeout=2)
                async with sessions() as db:
                    await db.execute(update(Channel).where(Channel.id == channel.id).values(
                        **oauth.credential_values(oauth._credentials(tokens("new-account", "new-refresh")))))
                    await db.commit()
                resume.set()
                assert (await refreshing).account_id == "new-account"
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "connected" and saved.oauth_error is None
                assert saved.oauth_refresh_token == "new-refresh" and saved.oauth_refresh_owner is None
    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["oauth_access_token", "oauth_refresh_token", "oauth_account_id", "oauth_expires_at"])
def test_missing_saved_credentials_are_unauthed_and_do_not_request_refresh(tmp_path, monkeypatch, field):
    async def exercise():
        async def no_network(request):
            raise AssertionError("Incomplete credentials must not be refreshed")
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, **{field: None})
            async with AsyncClient(transport=MockTransport(no_network)) as client:
                with pytest.raises(oauth.OAuthError) as caught:
                    await oauth.resolve_oauth_credentials(channel, client)
            assert caught.value.auth_status == "unauthed" and caught.value.status_code == 401
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "unauthed" and saved.oauth_refresh_owner is None
    asyncio.run(exercise())


def test_reauth_required_stays_stable_until_new_login_is_saved(tmp_path, monkeypatch):
    async def exercise():
        async def no_network(request):
            raise AssertionError("A rejected refresh token must not be reused")
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1,
                                 oauth_status="reauth_required", oauth_error="unsafe database error refresh-secret")
            async with AsyncClient(transport=MockTransport(no_network)) as client:
                with pytest.raises(oauth.OAuthError) as caught:
                    await oauth.resolve_oauth_credentials(channel, client)
                assert caught.value.status_code == 401 and caught.value.auth_status == "reauth_required"
                assert "refresh-secret" not in str(caught.value) and "unsafe database" not in str(caught.value)
                assert normalize_upstream_error(caught.value).fallback_allowed
                async with sessions() as db:
                    await db.execute(update(Channel).where(Channel.id == channel.id).values(
                        **oauth.credential_values(oauth._credentials(tokens("new-account", "new-refresh")))))
                    await db.commit()
                assert (await oauth.resolve_oauth_credentials(channel, client)).account_id == "new-account"
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "connected" and saved.oauth_error is None
    asyncio.run(exercise())


def test_rejection_saved_before_refresh_claim_cannot_reuse_rejected_token(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1)
            rejection_saved = []
            class RacingSession(AsyncSession):
                async def execute(self, statement, *args, **kwargs):
                    if isinstance(statement, Update) and not rejection_saved:
                        rejection_saved.append(channel.id)
                        async with sessions() as other:
                            await other.execute(update(Channel).where(Channel.id == channel.id).values(
                                oauth_status="reauth_required", oauth_error="Authorization rejected"))
                            await other.commit()
                    return await super().execute(statement, *args, **kwargs)
            racing = async_sessionmaker(sessions.kw["bind"], class_=RacingSession, expire_on_commit=False)
            monkeypatch.setattr(oauth, "async_session_maker", racing)
            async def no_network(request):
                raise AssertionError("A concurrent authentication rejection must prevent the refresh claim")
            async with AsyncClient(transport=MockTransport(no_network)) as client:
                with pytest.raises(oauth.OAuthError) as caught:
                    await oauth.resolve_oauth_credentials(channel, client)
            assert caught.value.auth_status == "reauth_required" and rejection_saved == [channel.id]
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "reauth_required" and saved.oauth_refresh_owner is None
    asyncio.run(exercise())


@pytest.mark.parametrize("old_status", ["error", "temporary_error"])
def test_authorization_check_refreshes_retryable_state_and_detail_reads_new_metadata(tmp_path, monkeypatch, old_status):
    async def exercise():
        calls = []
        async def upstream(request):
            calls.append(request)
            assert str(request.url) == oauth.TOKEN_URL and request.method == "POST"
            return Response(200, json=tokens("renewed-account", "renewed-refresh"))
        from rotor.api.admin import channels
        monkeypatch.setattr(channels.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() - 1,
                                 oauth_status=old_status, oauth_error="Previous timeout")
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                checked = await client.post(f"/api/admin/channels/{channel.id}/test")
                detail = await client.get(f"/api/admin/channels/{channel.id}")
            assert checked.status_code == 200 and checked.json()["ok"]
            assert checked.json()["check_type"] == "oauth"
            assert detail.status_code == 200 and detail.json()["auth_status"] == "connected"
            assert detail.json()["auth_error"] is None
            assert detail.json()["auth_expires_at"] > time.time() + 300
            assert len(calls) == 1
    asyncio.run(exercise())


def test_model_catalog_permission_denial_does_not_mark_global_authorization_invalid(tmp_path, monkeypatch):
    async def exercise():
        calls = []
        async def upstream(request):
            calls.append(request)
            assert request.method == "GET" and request.url.host == "chatgpt.com"
            return Response(403, json={"error": {"code": "permission_denied"}})
        from rotor.api.admin import channels
        monkeypatch.setattr(channels.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as client:
                denied = await client.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert denied.status_code == 400 and len(calls) == 1
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "connected" and saved.oauth_error is None
                assert saved.oauth_access_token == jwt() and saved.oauth_refresh_token == "refresh-secret"
    asyncio.run(exercise())
