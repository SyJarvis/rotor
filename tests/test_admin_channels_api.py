import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.api.admin.channels as channels_api
from rotor.api.admin.channels import router
from rotor.database import Base, get_db
from rotor.models.channel import Channel


def test_probe_models_error_detail_redacts_upstream_secrets(monkeypatch) -> None:
    async def exercise() -> dict:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            channel = Channel(
                name="leaky",
                type="openai",
                key="provider-secret-key",
                base_url="https://api.example.com/v1",
                models=[],
                model_mapping={},
                protocol="openai",
                extra={},
            )
            setup_db.add(channel)
            await setup_db.commit()
            await setup_db.refresh(channel)
            channel_id = channel.id

        def failing_fetch_model_list(**kwargs):
            import httpx
            request = httpx.Request("GET", "https://api.example.com/v1/models")
            response = httpx.Response(
                503,
                request=request,
                json={
                    "error": {
                        "message": "upstream exploded",
                        "api_key": "provider-secret-key",
                        "authorization": "Bearer provider-secret-key",
                    },
                },
            )
            raise httpx.HTTPStatusError(
                "upstream error", request=request, response=response
            )

        monkeypatch.setattr(
            channels_api, "_fetch_model_list", failing_fetch_model_list
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/admin/channels/{channel_id}/probe-models",
            )
        await engine.dispose()

        assert response.status_code == 400
        return response.json()

    detail = asyncio.run(exercise())

    detail_text = str(detail)
    assert "provider-secret-key" not in detail_text
    assert "[redacted]" in detail_text


def test_channel_test_error_field_redacts_upstream_secrets(monkeypatch) -> None:
    async def exercise() -> dict:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            channel = Channel(
                name="leaky-test",
                type="openai",
                key="provider-secret-key",
                base_url="https://api.example.com/v1",
                models=["model-a"],
                model_mapping={},
                protocol="openai",
                extra={},
            )
            setup_db.add(channel)
            await setup_db.commit()
            await setup_db.refresh(channel)
            channel_id = channel.id

        def failing_fetch_model_list(**kwargs):
            import httpx
            request = httpx.Request("GET", "https://api.example.com/v1/models")
            response = httpx.Response(
                500,
                request=request,
                json={
                    "error": {
                        "message": "token provider-secret-key is invalid",
                    },
                },
            )
            raise httpx.HTTPStatusError(
                "upstream error", request=request, response=response
            )

        monkeypatch.setattr(
            channels_api, "_fetch_model_list", failing_fetch_model_list
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/admin/channels/{channel_id}/test",
            )
        await engine.dispose()

        assert response.status_code == 200
        return response.json()

    payload = asyncio.run(exercise())

    assert payload["ok"] is False
    assert "provider-secret-key" not in payload["error"]
    assert "[redacted]" in payload["error"]


def test_probe_models_unsaved_uses_form_values(monkeypatch) -> None:
    async def exercise() -> None:
        captured = {}

        async def fake_fetch_model_list(**kwargs):
            captured.update(kwargs)
            return ["form-model-a", "form-model-b"]

        monkeypatch.setattr(channels_api, "_fetch_model_list", fake_fetch_model_list)
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/admin/channels/probe-models",
                json={
                    "base_url": "https://api.example.com/v1",
                    "key": "form-provider-key",
                    "type": "openai",
                    "protocol": "openai",
                    "extra": {
                        "models_path": "/models",
                        "auth_type": "bearer",
                        "headers": {"X-Provider-Account": "account-1"},
                    },
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["models"] == ["form-model-a", "form-model-b"]
        assert payload["raw_count"] == 2
        assert captured["base_url"] == "https://api.example.com/v1"
        assert captured["key"] == "form-provider-key"
        assert captured["extra_headers"] == {"X-Provider-Account": "account-1"}

    asyncio.run(exercise())


def test_probe_models_unsaved_error_redacts_form_key(monkeypatch) -> None:
    async def exercise() -> dict:
        def failing_fetch_model_list(**kwargs):
            import httpx
            request = httpx.Request("GET", "https://api.example.com/v1/models")
            response = httpx.Response(
                401,
                request=request,
                json={"error": {"message": "key form-provider-key is invalid"}},
            )
            raise httpx.HTTPStatusError(
                "upstream error", request=request, response=response
            )

        monkeypatch.setattr(
            channels_api, "_fetch_model_list", failing_fetch_model_list
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/admin/channels/probe-models",
                json={
                    "base_url": "https://api.example.com/v1",
                    "key": "form-provider-key",
                    "type": "openai",
                },
            )

        assert response.status_code == 400
        return response.json()

    detail = asyncio.run(exercise())

    detail_text = str(detail)
    assert "form-provider-key" not in detail_text
    assert "[redacted]" in detail_text


def test_probe_models_uses_saved_key_when_editing_channel(monkeypatch) -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            channel = Channel(
                name="existing",
                type="openai",
                key="saved-provider-key",
                base_url="https://api.example.com/v1",
                models=[],
                model_mapping={},
                protocol="openai",
                extra={"headers": {"X-Provider-Account": "account-1"}},
            )
            setup_db.add(channel)
            await setup_db.commit()
            await setup_db.refresh(channel)
            channel_id = channel.id

        captured = {}

        async def fake_fetch_model_list(**kwargs):
            captured.update(kwargs)
            return ["model-a"]

        monkeypatch.setattr(channels_api, "_fetch_model_list", fake_fetch_model_list)
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async def override_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/admin/channels/{channel_id}/probe-models",
            )
        await engine.dispose()

        assert response.status_code == 200
        assert response.json()["models"] == ["model-a"]
        assert captured["key"] == "saved-provider-key"
        assert captured["extra_headers"] == {"X-Provider-Account": "account-1"}

    asyncio.run(exercise())
