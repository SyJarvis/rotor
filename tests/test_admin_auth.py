import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin.auth import router as auth_router
from rotor.api.v1.models import router as models_router
from rotor.config import settings
from rotor.core.admin_auth import (
    ADMIN_CSRF_COOKIE,
    ADMIN_SESSION_COOKIE,
    ensure_default_admin,
    require_admin_access,
    verify_admin_password,
)
from rotor.database import Base, get_db
from rotor.main import app as rotor_app
from rotor.models.admin_auth import (
    AdminAuthEvent,
    AdminLoginThrottle,
    AdminSession,
    AdminUser,
)
from rotor.models.channel import Channel
from rotor.services.admin_security import (
    login_retry_after_seconds,
    record_login_failure,
)


class AdminAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.app = FastAPI()
        self.app.include_router(auth_router, prefix="/api/admin")
        protected = APIRouter()

        @protected.get("/protected")
        async def protected_get() -> dict[str, bool]:
            return {"ok": True}

        @protected.post("/protected")
        async def protected_post() -> dict[str, bool]:
            return {"ok": True}

        self.app.include_router(
            protected,
            prefix="/api/admin",
            dependencies=[Depends(require_admin_access)],
        )

        async def override_db():
            async with self.sessions() as db:
                yield db
                await db.commit()

        self.app.dependency_overrides[get_db] = override_db
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://test",
        )
        async with self.sessions() as db:
            self.admin = await ensure_default_admin(db)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()

    async def login(self, password: str = "123456"):
        return await self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": password},
        )

    async def test_default_admin_password_is_only_stored_as_hash(self) -> None:
        async with self.sessions() as db:
            stored = await db.get(AdminUser, self.admin.id)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.username, "admin")
        self.assertNotEqual(stored.password_hash, "123456")
        self.assertTrue(verify_admin_password("123456", stored.password_hash))
        self.assertTrue(stored.must_change_password)

    async def test_default_admin_creation_is_idempotent(self) -> None:
        async with self.sessions() as db:
            second = await ensure_default_admin(db)
            count = await db.scalar(select(func.count(AdminUser.id)))

        self.assertEqual(second.id, self.admin.id)
        self.assertEqual(count, 1)

    async def test_protected_admin_route_rejects_missing_credentials(self) -> None:
        response = await self.client.get("/api/admin/protected")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Session")

    async def test_login_rejects_invalid_password(self) -> None:
        response = await self.login("wrong-password")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "Invalid administrator credentials",
        )

    async def test_failed_login_is_persisted_in_auth_audit(self) -> None:
        await self.login("wrong-password")

        async with self.sessions() as db:
            event = await db.scalar(select(AdminAuthEvent))

        self.assertIsNotNone(event)
        self.assertEqual(event.username, "admin")
        self.assertEqual(event.event_type, "login_failed")
        self.assertFalse(event.success)
        self.assertEqual(event.reason, "invalid_credentials")
        self.assertEqual(event.client_ip, "127.0.0.1")

    async def test_login_lockout_is_scoped_by_username_and_ip(self) -> None:
        for _ in range(settings.ROTOR_ADMIN_LOGIN_MAX_FAILURES - 1):
            response = await self.login("wrong-password")
            self.assertEqual(response.status_code, 401)

        locked = await self.login("wrong-password")
        blocked_correct_password = await self.login()
        async with AsyncClient(
            transport=ASGITransport(
                app=self.app,
                client=("198.51.100.2", 12345),
            ),
            base_url="http://test",
        ) as other_client:
            other_ip = await other_client.post(
                "/api/admin/auth/login",
                json={"username": "admin", "password": "123456"},
            )
        async with self.sessions() as db:
            lockout_events = await db.scalar(
                select(func.count(AdminAuthEvent.id)).where(
                    AdminAuthEvent.event_type == "login_rate_limited"
                )
            )

        self.assertEqual(locked.status_code, 429)
        self.assertEqual(
            locked.headers["retry-after"],
            str(settings.ROTOR_ADMIN_LOGIN_LOCK_SECONDS),
        )
        self.assertEqual(blocked_correct_password.status_code, 429)
        self.assertEqual(other_ip.status_code, 200)
        self.assertEqual(lockout_events, 1)

    async def test_successful_login_clears_previous_failures(self) -> None:
        await self.login("wrong-password")

        response = await self.login()
        async with self.sessions() as db:
            throttle_count = await db.scalar(
                select(func.count(AdminLoginThrottle.id))
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(throttle_count, 0)

    async def test_expired_lockout_starts_a_new_failure_window(self) -> None:
        started_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
        username_key = "admin"
        client_ip = "203.0.113.10"
        async with self.sessions() as db:
            for _ in range(settings.ROTOR_ADMIN_LOGIN_MAX_FAILURES):
                retry_after = await record_login_failure(
                    db,
                    username_key=username_key,
                    client_ip=client_ip,
                    now=started_at,
                )
            after_lock = started_at + timedelta(
                seconds=settings.ROTOR_ADMIN_LOGIN_LOCK_SECONDS + 1
            )
            expired = await login_retry_after_seconds(
                db,
                username_key=username_key,
                client_ip=client_ip,
                now=after_lock,
            )
            next_retry = await record_login_failure(
                db,
                username_key=username_key,
                client_ip=client_ip,
                now=after_lock,
            )
            throttle = await db.scalar(
                select(AdminLoginThrottle).where(
                    AdminLoginThrottle.username_key == username_key,
                    AdminLoginThrottle.client_ip == client_ip,
                )
            )

        self.assertEqual(
            retry_after,
            settings.ROTOR_ADMIN_LOGIN_LOCK_SECONDS,
        )
        self.assertIsNone(expired)
        self.assertIsNone(next_retry)
        self.assertEqual(throttle.failure_count, 1)
        self.assertIsNone(throttle.locked_until)

    async def test_first_login_issues_session_and_requires_password_change(
        self,
    ) -> None:
        logged_in = await self.login()
        protected = await self.client.get("/api/admin/protected")

        cookies = logged_in.headers.get_list("set-cookie")
        session_cookie = next(
            value for value in cookies
            if value.startswith(f"{ADMIN_SESSION_COOKIE}=")
        )
        csrf_cookie = next(
            value for value in cookies
            if value.startswith(f"{ADMIN_CSRF_COOKIE}=")
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertTrue(logged_in.json()["must_change_password"])
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=strict", session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertEqual(protected.status_code, 403)
        self.assertEqual(
            protected.json()["detail"],
            "Administrator password change is required",
        )

    async def test_password_change_requires_matching_csrf_header(self) -> None:
        await self.login()

        response = await self.client.post(
            "/api/admin/auth/change-password",
            json={
                "current_password": "123456",
                "new_password": "correct horse battery staple",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid admin CSRF token")

    async def test_password_change_rejects_five_character_password(self) -> None:
        await self.login()
        csrf = self.client.cookies.get(ADMIN_CSRF_COOKIE)

        response = await self.client.post(
            "/api/admin/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "123456",
                "new_password": "12345",
            },
        )

        self.assertEqual(response.status_code, 422)

    async def test_failed_password_change_is_audited(self) -> None:
        await self.login()
        csrf = self.client.cookies.get(ADMIN_CSRF_COOKIE)

        response = await self.client.post(
            "/api/admin/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "wrong-password",
                "new_password": "654321",
            },
        )
        async with self.sessions() as db:
            event = await db.scalar(
                select(AdminAuthEvent).where(
                    AdminAuthEvent.event_type == "password_change_failed"
                )
            )

        self.assertEqual(response.status_code, 401)
        self.assertIsNotNone(event)
        self.assertEqual(event.reason, "invalid_current_password")

    async def test_six_character_password_unlocks_and_replaces_default(self) -> None:
        await self.login()
        csrf = self.client.cookies.get(ADMIN_CSRF_COOKIE)

        changed = await self.client.post(
            "/api/admin/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "123456",
                "new_password": "654321",
            },
        )
        protected = await self.client.get("/api/admin/protected")
        await self.client.post("/api/admin/auth/logout")
        old_login = await self.login("123456")
        new_login = await self.login("654321")

        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.json()["must_change_password"])
        self.assertEqual(protected.status_code, 200)
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    async def test_logout_revokes_server_session(self) -> None:
        await self.login()

        logged_out = await self.client.post("/api/admin/auth/logout")
        current = await self.client.get("/api/admin/auth/me")
        async with self.sessions() as db:
            session_count = await db.scalar(
                select(func.count(AdminSession.id))
            )

        self.assertEqual(logged_out.status_code, 204)
        self.assertEqual(current.status_code, 401)
        self.assertEqual(session_count, 0)

    async def test_auth_audit_endpoint_requires_completed_password_change(
        self,
    ) -> None:
        anonymous = await self.client.get("/api/admin/auth/audit-events")
        await self.login()
        before_change = await self.client.get(
            "/api/admin/auth/audit-events"
        )
        csrf = self.client.cookies.get(ADMIN_CSRF_COOKIE)
        await self.client.post(
            "/api/admin/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "123456",
                "new_password": "654321",
            },
        )

        audit = await self.client.get("/api/admin/auth/audit-events")

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(before_change.status_code, 403)
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(
            [event["event_type"] for event in audit.json()],
            ["password_changed", "login_succeeded"],
        )
        self.assertNotIn("session_id", audit.json()[0])

    async def test_logout_is_recorded_in_auth_audit(self) -> None:
        await self.login()

        await self.client.post("/api/admin/auth/logout")
        async with self.sessions() as db:
            event = await db.scalar(
                select(AdminAuthEvent).where(
                    AdminAuthEvent.event_type == "logout"
                )
            )

        self.assertIsNotNone(event)
        self.assertTrue(event.success)


def test_main_admin_routes_are_protected_but_probe_remains_public() -> None:
    async def exercise() -> tuple[int, int]:
        async with AsyncClient(
            transport=ASGITransport(app=rotor_app),
            base_url="http://test",
        ) as client:
            admin = await client.get("/api/admin/channels/presets")
            probe = await client.head("/anthropic/api/hello")
        return admin.status_code, probe.status_code

    assert asyncio.run(exercise()) == (401, 200)


def test_model_list_remains_public_without_rotor_token() -> None:
    async def exercise() -> tuple[int, list[str]]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(Channel(
                name="public-models",
                type="openai",
                key="provider-key",
                base_url="https://example.test/v1",
                models=["model-a"],
                model_mapping={},
                enabled=True,
                protocol="openai",
                extra={},
            ))
            await db.commit()

        app = FastAPI()
        app.include_router(models_router, prefix="/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/models")
        await engine.dispose()
        return response.status_code, [
            model["id"] for model in response.json()["data"]
        ]

    assert asyncio.run(exercise()) == (200, ["model-a"])
