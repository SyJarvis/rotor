import asyncio
import time

from httpx import ASGITransport, AsyncClient, ReadTimeout, Response
import pytest
from sqlalchemy import update

from rotor.models.channel import Channel
from rotor.services import chatgpt_oauth as oauth
from tests.test_chatgpt_oauth_backend import app_for, database, jwt, seed, tokens
from tests.test_chatgpt_oauth_catalog import mock_upstream, model


def test_saved_catalog_refreshes_a_rejected_unexpired_token_once(tmp_path, monkeypatch):
    async def exercise():
        requests = []

        async def upstream(request):
            requests.append(request)
            if request.method == "POST":
                assert str(request.url) == oauth.TOKEN_URL
                assert b"refresh_token=refresh-secret" in request.content
                return Response(200, json=tokens("rotated-account", "rotated-refresh"))
            if request.headers["authorization"] == "Bearer " + jwt():
                return Response(401, text="rejected upstream secret")
            assert request.headers["authorization"] == "Bearer " + jwt("rotated-account")
            return Response(200, json={"models": [model("discovered")]})

        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + 9 * 86400)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert response.status_code == 200
            assert response.json()["models"] == ["discovered"]
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_token == "rotated-refresh"
                assert saved.oauth_status == "connected" and saved.oauth_error is None
                assert saved.models == ["gpt-codex"]
                assert saved.oauth_refresh_owner is None and saved.oauth_refresh_lock_until is None
        assert [request.method for request in requests] == ["GET", "POST", "GET"]

    asyncio.run(exercise())


@pytest.mark.parametrize("failure,expected_status,auth_status", [
    ("invalid_grant", 400, "reauth_required"), ("transient", 503, "temporary_error"),
    ("second_401", 400, "reauth_required"), ("second_403", 400, "connected"),
    ("second_503", 503, "connected"),
])
def test_saved_catalog_retry_is_bounded_and_preserves_models_and_credentials(tmp_path, monkeypatch, failure, expected_status, auth_status):
    async def exercise():
        requests = []

        async def upstream(request):
            requests.append(request)
            if request.method == "POST":
                if failure == "invalid_grant":
                    return Response(400, json={"error": "invalid_grant", "secret": "refresh-secret"})
                if failure == "transient":
                    return Response(503, text="refresh-secret")
                return Response(200, json=tokens("rotated", "rotated-refresh"))
            status = 401 if len(requests) == 1 else int(failure.rsplit("_", 1)[1])
            return Response(status, text="refresh-secret " + jwt())

        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
                assert response.status_code == expected_status
                assert all(secret not in response.text for secret in ("refresh-secret", "rotated-refresh", jwt(), jwt("rotated")))
                if auth_status == "reauth_required":
                    count = len(requests)
                    repeated = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
                    assert repeated.status_code == 400 and len(requests) == count
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.models == ["gpt-codex"] and saved.auth_status == auth_status
                assert saved.oauth_refresh_token == ("rotated-refresh" if failure.startswith("second_") else "refresh-secret")
                assert saved.oauth_refresh_owner is None and saved.oauth_refresh_lock_until is None
                if saved.oauth_error:
                    assert "refresh-secret" not in saved.oauth_error
        assert [request.method for request in requests] == (["GET", "POST", "GET"] if failure.startswith("second_") else ["GET", "POST"])

    asyncio.run(exercise())


@pytest.mark.parametrize("failure,status", [("403", 400), ("503", 503), ("timeout", 503)])
def test_catalog_non_401_failure_does_not_refresh_or_invalidate_credentials(tmp_path, monkeypatch, failure, status):
    async def exercise():
        requests = []

        async def upstream(request):
            requests.append(request)
            if failure == "timeout":
                raise ReadTimeout("refresh-secret", request=request)
            return Response(int(failure), text="refresh-secret")

        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert response.status_code == status and "sign in again" not in response.text
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_status == "connected" and saved.oauth_error is None
                assert saved.oauth_refresh_token == "refresh-secret" and saved.models == ["gpt-codex"]
        assert [request.method for request in requests] == ["GET"]

    asyncio.run(exercise())


@pytest.mark.parametrize("replacement_stage", ["first_401", "refresh", "second_401", "active_refresh"])
def test_catalog_rejection_does_not_overwrite_or_rotate_a_concurrent_replacement(tmp_path, monkeypatch, replacement_stage):
    async def exercise():
        requests = []
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)

            async def replace():
                values = (dict(oauth_refresh_owner="another-refresh", oauth_refresh_lock_until=time.time() + 60)
                          if replacement_stage == "active_refresh" else oauth.credential_values(oauth._credentials(tokens("new-login", "new-refresh"))))
                async with sessions() as db:
                    await db.execute(update(Channel).where(Channel.id == channel.id).values(**values))
                    await db.commit()

            async def upstream(request):
                requests.append(request)
                if request.method == "POST":
                    assert b"refresh_token=refresh-secret" in request.content
                    if replacement_stage == "refresh":
                        await replace()
                        return Response(400, json={"error": "invalid_grant"})
                    return Response(200, json=tokens("rotated", "rotated-refresh"))
                if len(requests) == 1:
                    if replacement_stage == "first_401":
                        await replace()
                    return Response(401)
                if replacement_stage in {"second_401", "active_refresh"}:
                    await replace()
                    return Response(401)
                assert request.headers["authorization"] == "Bearer " + jwt("new-login")
                return Response(200, json={"models": [model("available")]})

            mock_upstream(monkeypatch, upstream)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert response.status_code == (400 if replacement_stage in {"second_401", "active_refresh"} else 200)
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.auth_status == "connected" and saved.oauth_error is None
                assert saved.models == ["gpt-codex"]
                if replacement_stage == "active_refresh":
                    assert saved.oauth_refresh_owner == "another-refresh"
                    assert saved.oauth_refresh_token == "rotated-refresh"
                else:
                    assert saved.oauth_refresh_token == "new-refresh" and saved.oauth_access_token == jwt("new-login")
        assert [request.method for request in requests] == (["GET", "GET"] if replacement_stage == "first_401" else ["GET", "POST", "GET"])

    asyncio.run(exercise())


@pytest.mark.parametrize("same_access_token", [False, True])
def test_concurrent_rejected_catalogs_share_one_refresh_even_when_access_token_is_unchanged(tmp_path, monkeypatch, same_access_token):
    async def exercise():
        requests = []
        both_rejected = asyncio.Event()
        initial_gets = 0
        rotated_account = "account-1" if same_access_token else "rotated"

        async def upstream(request):
            nonlocal initial_gets
            requests.append(request)
            if request.method == "POST":
                return Response(200, json=tokens(rotated_account, "rotated-refresh"))
            if initial_gets < 2:
                initial_gets += 1
                if initial_gets == 2:
                    both_rejected.set()
                await asyncio.wait_for(both_rejected.wait(), timeout=2)
                return Response(401)
            assert request.headers["authorization"] == "Bearer " + jwt(rotated_account)
            return Response(200, json={"models": [model("available")]})

        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                url = f"/api/admin/channels/{channel.id}/probe-models"
                responses = await asyncio.gather(admin.post(url), admin.post(url))
            assert all(response.status_code == 200 for response in responses)
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.models == ["gpt-codex"] and saved.auth_status == "connected"
                assert saved.oauth_refresh_token == "rotated-refresh" and saved.oauth_refresh_owner is None
        assert [request.method for request in requests].count("POST") == 1
        assert [request.method for request in requests].count("GET") == 4

    asyncio.run(exercise())


@pytest.mark.parametrize("reflected", [jwt(), "refresh-secret", "rotated-refresh"])
def test_catalog_retry_rejects_reflected_credentials_from_both_attempts(tmp_path, monkeypatch, reflected):
    async def exercise():
        requests = []

        async def upstream(request):
            requests.append(request)
            if request.method == "POST":
                return Response(200, json=tokens("rotated", "rotated-refresh"))
            if len(requests) == 1:
                return Response(401)
            return Response(200, json={"models": [model(reflected)]})

        mock_upstream(monkeypatch, upstream)
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert response.status_code == 502 and reflected not in response.text
            async with sessions() as db:
                assert (await db.get(Channel, channel.id)).models == ["gpt-codex"]
        assert len(requests) == 3

    asyncio.run(exercise())


def test_catalog_retry_binds_the_rejection_to_requested_credentials_not_later_database_metadata(tmp_path, monkeypatch):
    async def exercise():
        requests = []
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions)
            resolve = oauth.resolve_oauth_credentials

            async def resolve_then_replace(channel, client, **kwargs):
                credential = await resolve(channel, client, **kwargs)
                if kwargs.get("rejected_credential") is None:
                    async with sessions() as db:
                        await db.execute(update(Channel).where(Channel.id == channel.id).values(
                            **oauth.credential_values(oauth._credentials(tokens("account-1", "new-refresh", lifetime=7200)))))
                        await db.commit()
                return credential

            async def upstream(request):
                requests.append(request)
                assert request.method == "GET", "A later login with the same access token must not be rotated"
                return Response(401) if len(requests) == 1 else Response(200, json={"models": [model("available")]})

            monkeypatch.setattr(oauth, "resolve_oauth_credentials", resolve_then_replace)
            mock_upstream(monkeypatch, upstream)
            async with AsyncClient(transport=ASGITransport(app=app_for(sessions)), base_url="http://test") as admin:
                response = await admin.post(f"/api/admin/channels/{channel.id}/probe-models")
            assert response.status_code == 200 and len(requests) == 2
            async with sessions() as db:
                saved = await db.get(Channel, channel.id)
                assert saved.oauth_refresh_token == "new-refresh" and saved.auth_status == "connected"
                assert saved.models == ["gpt-codex"]

    asyncio.run(exercise())
