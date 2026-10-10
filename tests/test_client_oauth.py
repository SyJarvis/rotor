import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from rotor.api.admin.auth import router as auth_router
from rotor.api.client_oauth import router as oauth_router
from rotor.config import OAuthClientSettings, Settings, settings
from rotor.core.admin_auth import ADMIN_CSRF_COOKIE, hash_admin_password
from rotor.core.deps import get_current_token, get_optional_token
from rotor.core.exceptions import APIRouterException, api_router_exception_handler
from rotor.database import Base, create_database_engine, get_db
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.client_oauth import OAuthAuthorizationCode, OAuthCredential, OAuthGrant
from rotor.models.token import Token
from rotor.services.client_oauth import ACCESS_TTL, GRANT_TTL, pkce_challenge, secret_hash, utc_now


class ClientOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_database_engine(f"sqlite+aiosqlite:///{self.directory.name}/oauth.db")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.app = FastAPI()
        self.app.include_router(auth_router, prefix="/api")
        self.app.include_router(oauth_router)
        self.app.add_exception_handler(APIRouterException, api_router_exception_handler)

        @self.app.get("/inference")
        async def inference(token=Depends(get_current_token)):
            return {"token_id": token.id, "owner_id": token.owner_id}

        @self.app.get("/optional")
        async def optional(token=Depends(get_optional_token)):
            return {"token_id": token.id if token else None}

        async def override_db():
            async with self.sessions() as db:
                try:
                    yield db
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
        self.app.dependency_overrides[get_db] = override_db
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://127.0.0.1")
        async with self.sessions() as db:
            owner = AdminUser(username="alice", password_hash=hash_admin_password("long-password"), role="user",
                must_change_password=False, enabled=True, quota=None, allowed_channels=None, allowed_models=["allowed"])
            db.add(owner)
            await db.flush()
            token = Token(key="sk-account-key", name="base", owner_id=owner.id, is_base=True, enabled=True,
                quota=None, allowed_channels=None)
            db.add(token)
            db.add(Channel(name="channel", type="openai", key="upstream-key", base_url="https://upstream.invalid",
                models=["allowed", "other"], enabled=True))
            await db.flush()
            owner.default_token_id = token.id
            self.owner_id, self.token_id = owner.id, token.id
            await db.commit()
        self.verifier = "v" * 64
        self.params = {"response_type": "code", "client_id": "rotor-client",
            "redirect_uri": "http://127.0.0.1:1455/auth/callback", "scope": "inference", "state": "state-with-space &",
            "code_challenge": pkce_challenge(self.verifier), "code_challenge_method": "S256"}
        login = await self.client.post("/api/auth/login", json={"username": "alice", "password": "long-password"})
        self.assertEqual(login.status_code, 200, login.text)
        self.csrf = {"X-CSRF-Token": self.client.cookies.get(ADMIN_CSRF_COOKIE)}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self.directory.cleanup()

    async def authorize(self, **changes):
        response = await self.client.post("/api/oauth/authorization", json={**self.params, "approve": True, **changes}, headers=self.csrf)
        self.assertEqual(response.status_code, 200, response.text)
        parsed = parse_qs(urlsplit(response.json()["redirect_url"]).query)
        self.assertEqual(parsed["state"], [self.params["state"]])
        return parsed["code"][0]

    async def exchange(self, code, **changes):
        return await self.client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "rotor-client",
            "code": code, "redirect_uri": self.params["redirect_uri"], "code_verifier": self.verifier, **changes})

    async def credentials(self):
        response = await self.exchange(await self.authorize())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def refresh(self, credentials, **changes):
        return await self.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client",
            "refresh_token": credentials["refresh_token"], **changes})

    async def access(self, credentials, path="/inference"):
        return await self.client.get(path, headers={"Authorization": "Bearer " + credentials["access_token"]})

    async def mutate(self, model, identifier, **values):
        async with self.sessions() as db:
            await db.execute(update(model).where(model.id == identifier).values(**values))
            await db.commit()

    async def test_consent_uses_session_csrf_and_current_model_permissions(self):
        metadata = await self.client.get("/api/oauth/authorization", params=self.params)
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertEqual(metadata.json()["models"], ["allowed"])
        self.assertEqual(metadata.json()["username"], "alice")
        self.assertIsNone(metadata.json()["quota"])
        self.assertEqual(metadata.json()["used_quota"], 0)
        self.assertEqual(metadata.headers["cache-control"], "no-store")
        await self.mutate(AdminUser, self.owner_id, quota=50, used_quota=10)
        finite = await self.client.get("/api/oauth/authorization", params=self.params)
        self.assertEqual((finite.json()["quota"], finite.json()["used_quota"]), (50, 10))
        await self.mutate(AdminUser, self.owner_id, quota=0)
        self.assertEqual((await self.client.get("/api/oauth/authorization", params=self.params)).status_code, 429)
        await self.mutate(AdminUser, self.owner_id, quota=None, used_quota=0)
        forbidden = await self.client.post("/api/oauth/authorization", json={**self.params, "approve": True})
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.headers["cache-control"], "no-store")
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://127.0.0.1") as no_cookie:
            response = await no_cookie.get("/api/oauth/authorization", params=self.params)
            self.assertEqual(response.status_code, 401)
        await self.mutate(AdminUser, self.owner_id, must_change_password=True)
        blocked = await self.client.get("/api/oauth/authorization", params=self.params)
        self.assertEqual(blocked.status_code, 403)

    async def test_denial_and_strict_approve_do_not_issue_credentials(self):
        response = await self.client.post("/api/oauth/authorization", json={**self.params, "approve": False}, headers=self.csrf)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_qs(urlsplit(response.json()["redirect_url"]).query), {"error": ["access_denied"], "state": [self.params["state"]]})
        for approve in ("true", 1, None):
            with self.subTest(approve=approve):
                response = await self.client.post("/api/oauth/authorization", json={**self.params, "approve": approve}, headers=self.csrf)
                self.assertEqual(response.status_code, 400)
        async with self.sessions() as db:
            self.assertEqual((await db.scalars(select(OAuthAuthorizationCode))).all(), [])
            self.assertEqual((await db.scalars(select(OAuthGrant))).all(), [])

    async def test_invalid_authorization_parameters_never_redirect(self):
        cases = [{"redirect_uri": uri} for uri in ("https://evil.invalid/auth/callback", "http://localhost:1455/auth/callback",
            "http://127.0.0.1.evil.invalid/auth/callback", "http://user@127.0.0.1:1455/auth/callback", "http://127.0.0.1:1455/auth/callback?x=1",
            "http://127.0.0.1:1455/auth/callback#fragment", "http://127.0.0.1:1455/auth/callback%0d%0aLocation:evil", "javascript:alert(1)")]
        cases += [{"state": ""}, {"state": "s" * 513}, {"code_challenge_method": "plain"}, {"code_challenge": "short"},
            {"scope": "admin"}, {"client_id": "unknown"}, {"response_type": "token"}]
        for change in cases:
            with self.subTest(change=change):
                response = await self.client.get("/oauth/authorize", params={**self.params, **change})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn("location", response.headers)
                self.assertEqual(response.headers["cache-control"], "no-store")
        duplicate = await self.client.get("/oauth/authorize?" + urlencode(self.params) + "&state=duplicate")
        self.assertEqual(duplicate.status_code, 400)

    async def test_html_registration_query_is_page_only(self):
        for register in (None, "1"):
            params = self.params if register is None else {**self.params, "register": register}
            page = await self.client.get("/oauth/authorize", params=params)
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual(page.headers["referrer-policy"], "no-referrer")
            self.assertEqual(page.headers["x-frame-options"], "DENY")
            self.assertEqual(page.headers["content-security-policy"], "frame-ancestors 'none'; base-uri 'self'")
        invalid = await self.client.get("/oauth/authorize", params={**self.params, "register": "false"})
        self.assertEqual(invalid.status_code, 400)
        api = await self.client.get("/api/oauth/authorization", params={**self.params, "register": "1"})
        self.assertEqual(api.status_code, 400)

    async def test_authorized_apps_redirect_is_public_and_does_not_forward_credentials(self):
        credentials = await self.credentials()
        before = (await self.client.get("/api/oauth/grants")).json()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://127.0.0.1") as anonymous:
            for client in (self.client, anonymous):
                with self.subTest(authenticated=client is self.client):
                    response = await client.get("/oauth/apps", params={
                        "redirect_uri": "https://evil.invalid/callback", "settings": "other",
                        "access_token": credentials["access_token"], "refresh_token": credentials["refresh_token"],
                    }, follow_redirects=False)
                    self.assertEqual(response.status_code, 307)
                    self.assertEqual(response.headers["location"], "/?settings=security")
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(response.headers["pragma"], "no-cache")
                    self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                    self.assertEqual(response.content, b"")
                    self.assertNotIn(credentials["access_token"], str(response.headers))
                    self.assertNotIn(credentials["refresh_token"], str(response.headers))
            self.assertEqual((await anonymous.get("/api/oauth/grants")).status_code, 401)
        self.assertEqual((await self.client.get("/api/oauth/grants")).json(), before)
        self.assertEqual((await self.access(credentials)).status_code, 200)

    async def test_authorized_apps_redirect_reaches_settings_security_page(self):
        from rotor.main import frontend_index

        self.app.add_api_route("/", frontend_index)
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://127.0.0.1") as anonymous:
            response = await anonymous.get("/oauth/apps", follow_redirects=True)
        self.assertEqual([previous.status_code for previous in response.history], [307])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url.path, "/")
        self.assertEqual(response.url.query, b"settings=security")
        self.assertIn("text/html", response.headers["content-type"])

    async def test_pkce_binding_single_use_and_secret_storage(self):
        code = await self.authorize()
        other = OAuthClientSettings(name="Other", redirect_uris=["http://127.0.0.1/auth/callback"])
        with patch.object(settings, "ROTOR_OAUTH_CLIENTS", {**settings.ROTOR_OAUTH_CLIENTS, "other": other}):
            for changes in ({"code_verifier": "w" * 64}, {"redirect_uri": "http://127.0.0.1:1456/auth/callback"}, {"client_id": "other"}):
                response = await self.exchange(code, **changes)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_grant")
        response = await self.exchange(code)
        self.assertEqual(response.status_code, 200, response.text)
        credentials = response.json()
        self.assertEqual(credentials["expires_in"], ACCESS_TTL)
        self.assertEqual(credentials["token_type"], "Bearer")
        self.assertEqual((await self.exchange(code)).json()["error"], "invalid_grant")
        async with self.sessions() as db:
            stored_code = await db.scalar(select(OAuthAuthorizationCode))
            stored_credential = await db.scalar(select(OAuthCredential))
            grant = await db.scalar(select(OAuthGrant))
            self.assertEqual(stored_code.secret_hash, secret_hash(code))
            self.assertEqual(stored_credential.access_hash, secret_hash(credentials["access_token"]))
            self.assertEqual(stored_credential.refresh_hash, secret_hash(credentials["refresh_token"]))
            self.assertFalse(hasattr(grant, "token_id"))
            self.assertEqual(grant.owner_id, self.owner_id)
            self.assertEqual((grant.expires_at - grant.created_at).total_seconds(), GRANT_TTL)
            for secret in (code, credentials["access_token"], credentials["refresh_token"]):
                self.assertNotIn(secret, str(stored_code.__dict__) + str(stored_credential.__dict__) + str(grant.__dict__))
        self.assertEqual((await self.access(credentials)).json(), {"token_id": None, "owner_id": self.owner_id})
        self.assertIsNone((await self.access(credentials, "/optional")).json()["token_id"])

    async def test_authorization_code_claim_is_atomic(self):
        code = await self.authorize()
        responses = await asyncio.gather(self.exchange(code), self.exchange(code))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        async with self.sessions() as db:
            self.assertEqual(len((await db.scalars(select(OAuthGrant))).all()), 1)
            self.assertEqual(len((await db.scalars(select(OAuthCredential))).all()), 1)

    async def test_expired_code_and_expired_access_are_rejected(self):
        code = await self.authorize()
        async with self.sessions() as db:
            await db.execute(update(OAuthAuthorizationCode).values(expires_at=utc_now() - timedelta(seconds=1)))
            await db.commit()
        self.assertEqual((await self.exchange(code)).json()["error"], "invalid_grant")
        credentials = await self.credentials()
        async with self.sessions() as db:
            await db.execute(update(OAuthCredential).values(access_expires_at=utc_now() - timedelta(seconds=1)))
            await db.commit()
        denied = await self.access(credentials)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["www-authenticate"], "Bearer")
        self.assertEqual((await self.refresh(credentials)).status_code, 200)

    async def test_refresh_rotation_replay_revokes_whole_grant(self):
        old = await self.credentials()
        refreshed = await self.refresh(old)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        new = refreshed.json()
        self.assertNotEqual(new["refresh_token"], old["refresh_token"])
        self.assertEqual((await self.access(old)).status_code, 200)
        self.assertEqual((await self.access(new)).status_code, 200)
        replay = await self.refresh(old)
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual((await self.access(old)).status_code, 401)
        self.assertEqual((await self.access(new)).status_code, 401)
        self.assertEqual((await self.refresh(new)).json()["error"], "invalid_grant")
        async with self.sessions() as db:
            grant = await db.scalar(select(OAuthGrant))
            self.assertIsNotNone(grant.revoked_at)

    async def test_concurrent_refresh_replay_is_atomic_and_persisted(self):
        credentials = await self.credentials()
        responses = await asyncio.gather(self.refresh(credentials), self.refresh(credentials))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        winner = next(response.json() for response in responses if response.status_code == 200)
        self.assertEqual((await self.access(winner)).status_code, 401)
        async with self.sessions() as db:
            grant = await db.scalar(select(OAuthGrant))
            self.assertIsNotNone(grant.revoked_at)
            self.assertEqual(len((await db.scalars(select(OAuthCredential))).all()), 2)

    async def test_refresh_absolute_expiry_and_wrong_client_isolation(self):
        credentials = await self.credentials()
        other = OAuthClientSettings(name="Other", redirect_uris=["http://127.0.0.1/auth/callback"])
        with patch.object(settings, "ROTOR_OAUTH_CLIENTS", {**settings.ROTOR_OAUTH_CLIENTS, "other": other}):
            self.assertEqual((await self.refresh(credentials, client_id="other")).json()["error"], "invalid_grant")
            revoke = await self.client.post("/oauth/revoke", data={"client_id": "other", "token": credentials["refresh_token"]})
            self.assertEqual(revoke.status_code, 200)
        self.assertEqual((await self.access(credentials)).status_code, 200)
        async with self.sessions() as db:
            await db.execute(update(OAuthGrant).values(expires_at=utc_now() - timedelta(seconds=1)))
            await db.commit()
        self.assertEqual((await self.refresh(credentials)).json()["error"], "invalid_grant")
        self.assertEqual((await self.access(credentials)).status_code, 401)

    async def test_rotation_never_extends_absolute_grant_lifetime(self):
        credentials = await self.credentials()
        deadline = utc_now() + timedelta(seconds=60)
        async with self.sessions() as db:
            await db.execute(update(OAuthGrant).values(expires_at=deadline))
            await db.commit()
        response = await self.refresh(credentials)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.json()["expires_in"], 60)
        async with self.sessions() as db:
            grant = await db.scalar(select(OAuthGrant))
            new = await db.scalar(select(OAuthCredential).where(OAuthCredential.refresh_hash == secret_hash(response.json()["refresh_token"])))
            self.assertEqual(grant.expires_at.replace(tzinfo=deadline.tzinfo), deadline)
            self.assertEqual(new.access_expires_at, grant.expires_at)

    async def test_registered_https_redirect_requires_exact_match_and_ipv6_allows_port(self):
        client = OAuthClientSettings(name="Hosted Agent", redirect_uris=["https://agent.example/callback"])
        with patch.object(settings, "ROTOR_OAUTH_CLIENTS", {"hosted": client}):
            params = {**self.params, "client_id": "hosted", "redirect_uri": "https://agent.example/callback"}
            response = await self.client.get("/api/oauth/authorization", params=params)
            self.assertEqual(response.status_code, 200)
            for uri in ("https://agent.example:443/callback", "https://agent.example/callback/", "https://agent.example/Callback"):
                with self.subTest(uri=uri):
                    self.assertEqual((await self.client.get("/oauth/authorize", params={**params, "redirect_uri": uri})).status_code, 400)
        code = await self.authorize(redirect_uri="http://[::1]:17233/auth/callback")
        response = await self.exchange(code, redirect_uri="http://[::1]:17233/auth/callback")
        self.assertEqual(response.status_code, 200)

    async def test_revoke_access_refresh_and_browser_grant_are_isolated(self):
        for token_field in ("access_token", "refresh_token"):
            with self.subTest(token_field=token_field):
                credentials = await self.credentials()
                unrelated = await self.credentials()
                response = await self.client.post("/oauth/revoke", data={"client_id": "rotor-client", "token": credentials[token_field]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"")
                self.assertEqual((await self.access(credentials)).status_code, 401)
                self.assertEqual((await self.access(unrelated)).status_code, 200)
        unknown = await self.client.post("/oauth/revoke", data={"client_id": "rotor-client", "token": "unknown"})
        self.assertEqual(unknown.status_code, 200)
        items = (await self.client.get("/api/oauth/grants")).json()["items"]
        self.assertTrue(items)
        self.assertNotIn("token", json.dumps(items))
        forbidden = await self.client.delete(f"/api/oauth/grants/{items[0]['id']}")
        self.assertEqual(forbidden.status_code, 403)
        async with self.sessions() as db:
            alien = OAuthGrant(owner_id=999, client_id="rotor-client", scope="inference",
                account_auth_signature="h" * 64, created_at=utc_now(), expires_at=utc_now() + timedelta(days=1))
            db.add(alien)
            await db.commit()
            alien_id = alien.id
        self.assertEqual((await self.client.delete(f"/api/oauth/grants/{alien_id}", headers=self.csrf)).status_code, 404)
        deleted = await self.client.delete(f"/api/oauth/grants/{items[0]['id']}", headers=self.csrf)
        self.assertEqual(deleted.status_code, 204)
        self.assertNotIn(items[0]["id"], [item["id"] for item in (await self.client.get("/api/oauth/grants")).json()["items"]])

    async def test_owner_status_and_password_changes_apply_immediately(self):
        credentials = await self.credentials()
        for model, changes, reset in (
            (AdminUser, {"enabled": False}, {"enabled": True}),
            (AdminUser, {"must_change_password": True}, {"must_change_password": False}),
        ):
            identifier = self.owner_id if model is AdminUser else self.token_id
            with self.subTest(changes=changes):
                await self.mutate(model, identifier, **changes)
                self.assertEqual((await self.access(credentials)).status_code, 401)
                self.assertEqual((await self.refresh(credentials)).json()["error"], "invalid_grant")
                await self.mutate(model, identifier, **reset)
                self.assertEqual((await self.access(credentials)).status_code, 200)
        await self.mutate(AdminUser, self.owner_id, password_hash=hash_admin_password("different-password"))
        self.assertEqual((await self.access(credentials)).status_code, 401)
        self.assertEqual((await self.refresh(credentials)).json()["error"], "invalid_grant")

    async def test_password_change_during_code_exchange_is_rejected(self):
        code = await self.authorize()
        await self.mutate(AdminUser, self.owner_id, password_hash=hash_admin_password("different-password"))
        self.assertEqual((await self.exchange(code)).json()["error"], "invalid_grant")

    async def test_quota_is_temporary_and_failed_refresh_rolls_back_claim(self):
        credentials = await self.credentials()
        for model, identifier in ((AdminUser, self.owner_id),):
            with self.subTest(model=model):
                await self.mutate(model, identifier, quota=0)
                self.assertEqual((await self.access(credentials)).status_code, 429)
                response = await self.refresh(credentials)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json()["error"], "temporarily_unavailable")
                async with self.sessions() as db:
                    old = await db.scalar(select(OAuthCredential).where(OAuthCredential.refresh_hash == secret_hash(credentials["refresh_token"])))
                    self.assertIsNone(old.rotated_at)
                await self.mutate(model, identifier, quota=None)
                response = await self.refresh(credentials)
                self.assertEqual(response.status_code, 200, response.text)
                credentials = response.json()

    async def test_account_without_any_key_can_authorize_exchange_refresh_and_use_inference(self):
        await self.mutate(AdminUser, self.owner_id, default_token_id=None)
        async with self.sessions() as db:
            await db.execute(delete(Token))
            await db.commit()
        metadata = await self.client.get("/api/oauth/authorization", params=self.params)
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertEqual(metadata.json()["models"], ["allowed"])
        credentials = await self.credentials()
        self.assertEqual((await self.access(credentials)).json(), {"token_id": None, "owner_id": self.owner_id})
        refreshed = await self.refresh(credentials)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual((await self.access(refreshed.json())).status_code, 200)
        async with self.sessions() as db:
            self.assertEqual((await db.scalars(select(Token))).all(), [])

    async def test_key_status_default_and_deletion_do_not_affect_oauth(self):
        credentials = await self.credentials()
        for change in ({"enabled": False}, {"expired": True}, {"quota": 0}, {"owner_id": None},
                       {"allowed_channels": []}, {"allowed_models": []}):
            with self.subTest(change=change):
                await self.mutate(Token, self.token_id, **change)
                self.assertEqual((await self.access(credentials)).status_code, 200)
        await self.mutate(AdminUser, self.owner_id, default_token_id=999)
        self.assertEqual((await self.access(credentials)).status_code, 200)
        async with self.sessions() as db:
            await db.execute(delete(Token))
            await db.commit()
        refreshed = await self.refresh(credentials)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual((await self.access(refreshed.json())).status_code, 200)
        self.assertEqual((await self.client.get("/api/oauth/authorization", params=self.params)).status_code, 200)

    async def test_client_removal_blocks_authorization(self):
        credentials = await self.credentials()
        with patch.object(settings, "ROTOR_OAUTH_CLIENTS", {}):
            self.assertEqual((await self.access(credentials)).status_code, 401)
            self.assertEqual((await self.refresh(credentials)).json()["error"], "invalid_client")

    async def test_code_refresh_and_x_api_key_cannot_authenticate_inference(self):
        code = await self.authorize()
        credentials = await self.credentials()
        for secret in (code, credentials["refresh_token"]):
            response = await self.client.get("/inference", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(response.status_code, 401)
            optional = await self.client.get("/optional", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(optional.status_code, 401)
        response = await self.client.get("/inference", headers={"x-api-key": credentials["access_token"]})
        self.assertEqual(response.status_code, 401)
        api_key = await self.client.get("/inference", headers={"x-api-key": "sk-account-key"})
        self.assertEqual(api_key.status_code, 200)

    async def test_form_json_bounds_duplicates_and_public_client_auth(self):
        invalid_forms = [("grant_type=refresh_token&client_id=rotor-client&client_id=rotor-client", "application/x-www-form-urlencoded"),
            ("grant_type=refresh_token&refresh_token=%QQ", "application/x-www-form-urlencoded"),
            ("x=" + "x" * 8192, "application/x-www-form-urlencoded"), ("{}", "application/json"),
            ("grant_type=password", "application/x-www-form-urlencoded"),
            ("grant_type=refresh_token&client_secret=s&client_id=rotor-client", "application/x-www-form-urlencoded")]
        for body, content_type in invalid_forms:
            with self.subTest(body=body[:50]):
                response = await self.client.post("/oauth/token", content=body, headers={"Content-Type": content_type})
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
                self.assertEqual(response.headers["cache-control"], "no-store")
        basic = await self.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client"},
            headers={"Authorization": "Basic secret"})
        self.assertEqual(basic.json()["error"], "invalid_client")
        duplicate_json = json.dumps({**self.params, "approve": True})[:-1] + ',"approve":false}'
        response = await self.client.post("/api/oauth/authorization", content=duplicate_json,
            headers={**self.csrf, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)

    async def test_metadata_uses_configured_issuer_and_only_supported_capabilities(self):
        with patch.object(settings, "ROTOR_OAUTH_ISSUER", "https://rotor.example/prefix"):
            response = await self.client.get("/.well-known/oauth-authorization-server")
        metadata = response.json()
        self.assertEqual(metadata["issuer"], "https://rotor.example/prefix")
        self.assertEqual(metadata["token_endpoint"], "https://rotor.example/prefix/oauth/token")
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(metadata["token_endpoint_auth_methods_supported"], ["none"])
        self.assertNotIn("jwks_uri", metadata)
        self.assertNotIn("device_authorization_endpoint", metadata)


class ClientOAuthSettingsTests(unittest.TestCase):
    def test_registry_rejects_insecure_or_ambiguous_uris(self):
        for uri in ("http://example.com/cb", "http://localhost/cb", "https://user:pass@example.com/cb", "https://example.com/cb?state=x",
                    "https://example.com/cb#", "https://example.com/cb%0d%0aevil", "https://example.com\\evil/cb"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                OAuthClientSettings(name="client", redirect_uris=[uri])
        client = OAuthClientSettings(name="client", redirect_uris=["https://example.com/cb", "http://[::1]/cb"])
        self.assertEqual(len(client.redirect_uris), 2)

    def test_environment_json_and_https_issuer_validation(self):
        with patch.dict("os.environ", {"ROTOR_OAUTH_CLIENTS": json.dumps({"agent": {"name": "Agent", "redirect_uris": ["https://agent.example/cb"]}})}, clear=False):
            configured = Settings(_env_file=None)
        self.assertEqual(configured.ROTOR_OAUTH_CLIENTS["agent"].name, "Agent")
        for issuer in ("http://rotor.example", "https://user@rotor.example", "https://rotor.example?q=1", "https://rotor.example#bad"):
            with self.subTest(issuer=issuer), self.assertRaises(ValueError):
                Settings(_env_file=None, ROTOR_OAUTH_ISSUER=issuer)
