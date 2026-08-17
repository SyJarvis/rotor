import asyncio

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin.mcp_control_keys import router as admin_router
from rotor.core.control_auth import (
    ActorContext,
    ControlAPIException,
    control_api_exception_handler,
    require_control_scope,
)
from rotor.database import Base, get_db


def test_mcp_control_key_is_immediately_usable_and_can_be_disabled() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(admin_router, prefix="/api/admin")

        @app.get("/api/control/v1/channels")
        async def control_channels(
            actor: ActorContext = Depends(
                require_control_scope("channel:read")
            ),
        ) -> dict[str, str]:
            return {"actor_id": actor.actor_id}

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/admin/mcp-control-keys",
                json={"name": "Codex"},
            )
            payload = created.json()
            authorized = await client.get(
                "/api/control/v1/channels",
                headers={"Authorization": f"Bearer {payload['key']}"},
            )
            listed = await client.get("/api/admin/mcp-control-keys")
            disabled = await client.post(
                f"/api/admin/mcp-control-keys/{payload['id']}/disable"
            )
            rejected = await client.get(
                "/api/control/v1/channels",
                headers={"Authorization": f"Bearer {payload['key']}"},
            )
        await engine.dispose()

        assert created.status_code == 201
        assert payload["key"].startswith("rck_")
        assert payload["key"] not in str(listed.json())
        assert authorized.status_code == 200
        assert authorized.json()["actor_id"] == (
            f"mcp-control-key-{payload['id']}"
        )
        assert disabled.status_code == 200
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == (
            "control_invalid_credential"
        )

    asyncio.run(exercise())
