import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.v1.anthropic_models import router
from rotor.config import settings
from rotor.core.exceptions import APIRouterException, api_router_exception_handler
from rotor.database import Base, get_db
from rotor.models.channel import Channel
from rotor.models.token import Token


KEY = settings.API_KEY_PREFIX + "catalog-test-credential"


@asynccontextmanager
async def catalog_client(*, allowed_channels=None, token_enabled=True):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            Channel(id=1, name="native", type="anthropic", protocol="anthropic", key="provider-secret",
                    base_url="https://secret.example", models=["b", "d"], model_mapping={"a": "internal-native"}),
            Channel(id=2, name="convertible", type="openai", protocol="openai", key="other-secret",
                    base_url="https://other.example", models=["b", "f"], model_mapping={"c": "internal-chat", "e": "internal-e"}),
            Channel(id=3, name="disabled", type="anthropic", enabled=False, key="disabled-secret",
                    base_url="https://disabled.example", models=["hidden-disabled"], model_mapping={"hidden-alias": "hidden-value"}),
            Token(key=KEY, name="catalog", enabled=token_enabled, allowed_channels=allowed_channels),
        ])
        await db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/anthropic/v1")
    app.add_exception_handler(APIRouterException, api_router_exception_handler)

    async def override_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        await engine.dispose()


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "invalid"}, {"Authorization": "Bearer invalid"}])
def test_model_discovery_requires_rotor_authentication(headers):
    async def exercise():
        async with catalog_client() as client:
            result = await client.get("/anthropic/v1/models", headers=headers)
            assert result.status_code == 401
            assert "provider-secret" not in result.text
    asyncio.run(exercise())


@pytest.mark.parametrize("header", ["x-api-key", "Authorization"])
def test_model_discovery_is_local_scoped_and_uses_only_logical_ids(header):
    async def exercise():
        async with catalog_client(allowed_channels=[1]) as client:
            result = await client.get("/anthropic/v1/models", headers={header: KEY if header == "x-api-key" else f"Bearer {KEY}"})
            assert result.status_code == 200
            payload = result.json()
            assert payload["data"] == [
                {"id": model, "type": "model", "display_name": model, "created_at": "1970-01-01T00:00:00Z"}
                for model in ["a", "b", "d"]
            ]
            assert (payload["first_id"], payload["last_id"], payload["has_more"]) == ("a", "d", False)
            for secret in ["internal", "hidden", "secret", "example", "convertible"]:
                assert secret not in result.text
    asyncio.run(exercise())


def test_forward_and_backward_pages_are_stable_and_directional():
    async def exercise():
        async with catalog_client() as client:
            async def page(**params):
                result = await client.get("/anthropic/v1/models", headers={"x-api-key": KEY}, params=params)
                assert result.status_code == 200
                body = result.json()
                return [model["id"] for model in body["data"]], body
            ids, first = await page(limit=2)
            assert ids == ["a", "b"] and first["has_more"]
            ids, second = await page(limit=2, after_id=first["last_id"])
            assert ids == ["c", "d"] and second["has_more"]
            ids, third = await page(limit=2, after_id=second["last_id"])
            assert ids == ["e", "f"] and not third["has_more"]
            ids, back = await page(limit=2, before_id=third["first_id"])
            assert ids == ["c", "d"] and back["has_more"]
            ids, back = await page(limit=2, before_id=back["first_id"])
            assert ids == ["a", "b"] and not back["has_more"]
            ids, empty = await page(after_id="f")
            assert ids == [] and empty == {"data": [], "first_id": None, "last_id": None, "has_more": False}
            ids, empty = await page(before_id="a")
            assert ids == [] and not empty["has_more"]
            ids, full = await page()
            assert ids == ["a", "b", "c", "d", "e", "f"] and not full["has_more"]
    asyncio.run(exercise())


@pytest.mark.parametrize("params,status", [
    ({"limit": 0}, 422), ({"limit": 1001}, 422), ({"limit": "bad"}, 422),
    ({"after_id": "a", "before_id": "b"}, 400), ({"after_id": "unknown"}, 400),
    ({"before_id": "hidden-disabled"}, 400), ({"after_id": ""}, 400),
])
def test_invalid_pagination_does_not_echo_cursor(params, status):
    async def exercise():
        async with catalog_client() as client:
            result = await client.get("/anthropic/v1/models", headers={"x-api-key": KEY}, params=params)
            assert result.status_code == status
            if status == 400:
                assert "hidden-disabled" not in result.text and "unknown" not in result.text
    asyncio.run(exercise())


def test_hidden_and_nonexistent_cursor_errors_are_identical_and_disabled_token_rejected():
    async def exercise():
        async with catalog_client(allowed_channels=[1]) as client:
            hidden = await client.get("/anthropic/v1/models?after_id=c", headers={"x-api-key": KEY})
            unknown = await client.get("/anthropic/v1/models?after_id=unknown", headers={"x-api-key": KEY})
            assert hidden.status_code == unknown.status_code == 400
            assert hidden.json() == unknown.json()
        async with catalog_client(allowed_channels=[999]) as client:
            empty = await client.get("/anthropic/v1/models", headers={"x-api-key": KEY})
            assert empty.json() == {"data": [], "first_id": None, "last_id": None, "has_more": False}
        async with catalog_client(token_enabled=False) as client:
            result = await client.get("/anthropic/v1/models", headers={"x-api-key": KEY})
            assert result.status_code == 401
    asyncio.run(exercise())


def test_application_registers_anthropic_model_router():
    from rotor.main import app
    assert "get" in app.openapi()["paths"]["/anthropic/v1/models"]
