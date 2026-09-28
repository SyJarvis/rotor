import asyncio
from types import SimpleNamespace

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


def test_routing_state_endpoint_reports_cooldowns_without_secrets(monkeypatch) -> None:
    _reset_routing_engine()

    async def exercise() -> dict:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as setup_db:
            channel = Channel(
                name="cooling",
                type="openai",
                key="provider-secret-key",
                base_url="https://api.example.com/v1",
                models=["model"],
                model_mapping={},
                protocol="openai",
                extra={},
            )
            setup_db.add(channel)
            await setup_db.commit()
            await setup_db.refresh(channel)
            channel_id = channel.id

        fake_engine = SimpleNamespace(
            routing_state=lambda: [
                ("model", channel_id, {
                    "phase": "cooldown",
                    "remaining_seconds": 17,
                    "cooldown_seconds": 30.0,
                }),
                ("model", 99, {
                    "phase": "probe",
                    "remaining_seconds": 0,
                    "cooldown_seconds": 30.0,
                }),
            ],
        )
        monkeypatch.setattr(channels_api, "routing_engine", fake_engine)
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
            response = await client.get("/api/admin/channels/routing-state")
        await engine.dispose()

        assert response.status_code == 200
        return response.json()

    payload = asyncio.run(exercise())

    assert payload == {"cooldowns": [
        {
            "model": "model",
            "channel_id": payload["cooldowns"][0]["channel_id"],
            "channel_name": "cooling",
            "phase": "cooldown",
            "remaining_seconds": 17,
            "cooldown_seconds": 30.0,
            "known_channel": True,
        },
        {
            "model": "model",
            "channel_id": 99,
            "channel_name": None,
            "phase": "probe",
            "remaining_seconds": 0,
            "cooldown_seconds": 30.0,
            "known_channel": False,
        },
    ]}
    assert "provider-secret-key" not in str(payload)


async def _make_channel(sessions, **overrides) -> int:
    fields = dict(
        name="cooling",
        type="openai",
        key="provider-secret-key",
        base_url="https://api.example.com/v1",
        models=["model-a", "model-b"],
        model_mapping={},
        protocol="openai",
        extra={},
    )
    fields.update(overrides)
    async with sessions() as setup_db:
        channel = Channel(**fields)
        setup_db.add(channel)
        await setup_db.commit()
        await setup_db.refresh(channel)
        return channel.id


async def _reset_cooldown(sessions, channel_id, body):
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
        return await client.post(
            f"/api/admin/channels/{channel_id}/cooldown/reset", json=body
        )


def _reset_routing_engine() -> None:
    """The routing engine is a process-wide singleton; isolate each test."""
    from rotor.gateway.routing import routing_engine
    for model, channel_id, _ in routing_engine.routing_state():
        routing_engine.clear_cooldown(model, channel_id)
    assert routing_engine.routing_state() == []


def test_reset_cooldown_releases_every_model_on_the_channel() -> None:
    _reset_routing_engine()

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        channel_id = await _make_channel(sessions)

        from rotor.gateway.routing import routing_engine
        routing_engine.mark_unavailable(
            "model-a", SimpleNamespace(id=channel_id, extra={})
        )
        routing_engine.mark_unavailable(
            "model-b", SimpleNamespace(id=channel_id, extra={})
        )
        routing_engine.mark_unavailable(
            "other", SimpleNamespace(id=channel_id + 100, extra={})
        )

        response = await _reset_cooldown(sessions, channel_id, {})
        remaining = {model for model, _, _ in routing_engine.routing_state()}
        await engine.dispose()
        return response, remaining

    response, remaining = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json() == {"channel_id": 1, "released": ["model-a", "model-b"]}
    assert remaining == {"other"}


def test_reset_cooldown_can_target_one_model() -> None:
    _reset_routing_engine()

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        channel_id = await _make_channel(sessions)

        from rotor.gateway.routing import routing_engine
        routing_engine.mark_unavailable(
            "model-a", SimpleNamespace(id=channel_id, extra={})
        )
        routing_engine.mark_unavailable(
            "model-b", SimpleNamespace(id=channel_id, extra={})
        )

        response = await _reset_cooldown(sessions, channel_id, {"model": "model-a"})
        remaining = {model for model, _, _ in routing_engine.routing_state()}
        await engine.dispose()
        return response, remaining

    response, remaining = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json() == {"channel_id": 1, "released": ["model-a"]}
    assert remaining == {"model-b"}


def test_reset_cooldown_unknown_model_matches_no_entry() -> None:
    _reset_routing_engine()

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        channel_id = await _make_channel(sessions)

        from rotor.gateway.routing import routing_engine
        routing_engine.mark_unavailable(
            "model-a", SimpleNamespace(id=channel_id, extra={})
        )

        hit = await _reset_cooldown(sessions, channel_id, {"model": "model-a"})
        miss = await _reset_cooldown(sessions, channel_id, {"model": "model-a"})
        await engine.dispose()
        return hit, miss

    hit, miss = asyncio.run(exercise())

    assert hit.status_code == 200
    assert miss.status_code == 404
    assert "model-a" in miss.json()["detail"]


def test_reset_cooldown_unknown_channel_is_404() -> None:
    _reset_routing_engine()

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _make_channel(sessions)
        response = await _reset_cooldown(sessions, 999, {})
        await engine.dispose()
        return response

    response = asyncio.run(exercise())

    assert response.status_code == 404


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
