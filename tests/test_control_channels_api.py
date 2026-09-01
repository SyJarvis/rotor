import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.control.channels import router
from rotor.core.control_auth import (
    ControlAuthConfig,
    ControlAPIException,
    control_api_exception_handler,
    get_control_auth_config,
)
from rotor.database import Base, get_db
from rotor.models.channel import Channel


def _auth_config(scopes: set[str]) -> ControlAuthConfig:
    return ControlAuthConfig(
        token="control-token",
        actor_id="admin-1",
        client_id="rotor-mcp",
        scopes=frozenset(scopes),
    )


def _channel(
    *,
    name: str,
    key: str,
    base_url: str,
    models: list[str],
    enabled: bool,
    protocol: str,
    priority: int,
    resource_scopes: dict[str, str] | None = None,
) -> Channel:
    return Channel(
        name=name,
        type="openai" if protocol == "openai" else "anthropic",
        key=key,
        base_url=base_url,
        models=models,
        model_mapping={models[0]: f"provider-{models[0]}"},
        priority=priority,
        weight=1,
        enabled=enabled,
        test_only=False,
        protocol=protocol,
        extra={
            "headers": {
                "Authorization": "Bearer secret-header",
            },
            **(resource_scopes or {}),
        },
    )


def test_control_channels_require_scope_filter_and_exclude_secrets() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            setup_db.add_all([
                _channel(
                    name="primary",
                    key="provider-secret-key",
                    base_url=(
                        "https://user:password@api.example.com:8443/v1"
                        "?token=secret-query"
                    ),
                    models=["model-a"],
                    enabled=True,
                    protocol="openai",
                    priority=10,
                    resource_scopes={
                        "cache_scope": "provider/account-a/cache",
                        "capacity_scope": "provider/account-a/capacity",
                        "billing_scope": "provider/account-a/billing",
                    },
                ),
                _channel(
                    name="disabled",
                    key="other-secret-key",
                    base_url="https://other.example.com/v1",
                    models=["model-b"],
                    enabled=False,
                    protocol="anthropic",
                    priority=1,
                ),
            ])
            await setup_db.commit()

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(router, prefix="/api/control/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        active_scopes = {"value": {"request_trace:read"}}
        app.dependency_overrides[get_control_auth_config] = lambda: (
            _auth_config(active_scopes["value"])
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            forbidden = await client.get(
                "/api/control/v1/channels",
                headers={"Authorization": "Bearer control-token"},
            )
            active_scopes["value"] = {"channel:read"}
            allowed = await client.get(
                "/api/control/v1/channels",
                params={
                    "enabled": "true",
                    "model": "model-a",
                    "protocol": "openai",
                    "limit": 10,
                },
                headers={
                    "Authorization": "Bearer control-token",
                    "X-Request-Id": "control-channels-1",
                },
            )
        await engine.dispose()

        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "control_scope_required"
        assert allowed.status_code == 200
        assert allowed.json()["request_id"] == "control-channels-1"
        assert allowed.json()["page"] == {
            "next_cursor": None,
            "has_more": False,
            "limit": 10,
        }
        assert len(allowed.json()["data"]) == 1
        channel = allowed.json()["data"][0]
        assert channel["name"] == "primary"
        assert channel["base_url_origin"] == "https://api.example.com:8443"
        assert channel["secret_state"] == {"has_key": True}
        assert channel["cache_scope"] == "provider/account-a/cache"
        assert channel["capacity_scope"] == "provider/account-a/capacity"
        assert channel["billing_scope"] == "provider/account-a/billing"
        serialized = allowed.text
        for secret in (
            "provider-secret-key",
            "secret-header",
            "secret-query",
            "password",
        ):
            assert secret not in serialized
        assert "extra" not in channel
        assert "key" not in channel

    asyncio.run(exercise())


def test_control_channels_cursor_and_single_channel_errors() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            setup_db.add_all([
                _channel(
                    name="first",
                    key="secret-1",
                    base_url="https://first.example.com/v1",
                    models=["model-a"],
                    enabled=True,
                    protocol="openai",
                    priority=10,
                ),
                _channel(
                    name="second",
                    key="secret-2",
                    base_url="https://second.example.com/v1",
                    models=["model-b"],
                    enabled=True,
                    protocol="openai",
                    priority=1,
                ),
            ])
            await setup_db.commit()

        app = FastAPI()
        app.add_exception_handler(
            ControlAPIException,
            control_api_exception_handler,
        )
        app.include_router(router, prefix="/api/control/v1")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_control_auth_config] = lambda: (
            _auth_config({"channel:read"})
        )
        headers = {"Authorization": "Bearer control-token"}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first_page = await client.get(
                "/api/control/v1/channels",
                params={"limit": 1},
                headers=headers,
            )
            cursor = first_page.json()["page"]["next_cursor"]
            second_page = await client.get(
                "/api/control/v1/channels",
                params={"limit": 1, "cursor": cursor},
                headers=headers,
            )
            mismatched_cursor = await client.get(
                "/api/control/v1/channels",
                params={
                    "limit": 1,
                    "cursor": cursor,
                    "enabled": "false",
                },
                headers=headers,
            )
            channel_id = first_page.json()["data"][0]["id"]
            detail = await client.get(
                f"/api/control/v1/channels/{channel_id}",
                headers=headers,
            )
            missing = await client.get(
                "/api/control/v1/channels/9999",
                headers=headers,
            )
        await engine.dispose()

        assert first_page.status_code == 200
        assert first_page.json()["page"]["has_more"] is True
        assert first_page.json()["data"][0]["name"] == "first"
        assert second_page.status_code == 200
        assert second_page.json()["data"][0]["name"] == "second"
        assert mismatched_cursor.status_code == 400
        assert mismatched_cursor.json()["error"]["code"] == (
            "invalid_channel_query"
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["name"] == "first"
        assert detail.json()["data"]["cache_scope"] == f"channel:{channel_id}"
        assert detail.json()["data"]["capacity_scope"] == f"channel:{channel_id}"
        assert detail.json()["data"]["billing_scope"] == f"channel:{channel_id}"
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "channel_not_found"

    asyncio.run(exercise())
