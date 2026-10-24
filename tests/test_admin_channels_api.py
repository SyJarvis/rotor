import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.api.admin.channels as channels_api
from rotor.api.admin.channels import router
from rotor.database import Base, get_db
import rotor.models.admin_auth  # noqa: F401 - registers admin_users for Base.metadata.create_all
from rotor.models.channel import Channel
from rotor.core.exceptions import UpstreamProtocolError


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


@pytest.mark.parametrize("method,path", [("POST", "/api/admin/channels"), ("PUT", "/api/admin/channels/1")])
def test_channel_api_rejects_invalid_capabilities_before_saving(method, path) -> None:
    async def exercise():
        app = FastAPI()
        app.include_router(router, prefix="/api/admin")

        async def override_db():
            yield object()

        app.dependency_overrides[get_db] = override_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, json={
                "name": "invalid", "type": "openai", "key": "unused",
                "base_url": "https://mock.invalid/v1", "extra": {"capabilities": 123},
            })

    response = asyncio.run(exercise())

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "extra"]
    assert "extra.capabilities must be a list of strings" in response.json()["detail"][0]["msg"]


def _mock_stream_probe(monkeypatch, chunks, error=None):
    response = SimpleNamespace(status_code=200, close_count=0)
    observed = []

    async def close():
        response.close_count += 1

    response.aclose = close

    class FakeAdapter:
        async def make_request(self, request, timeout=None):
            assert request.stream is True
            return response

        async def stream_convert_response(self, response, request):
            for chunk in chunks:
                observed.append(chunk)
                yield chunk
            if error is not None:
                raise error

    monkeypatch.setattr(channels_api.AdapterFactory, "create_adapter", lambda channel, client: FakeAdapter())
    channel = SimpleNamespace(key="unused")

    def run():
        return asyncio.run(channels_api._probe_generation_capability(
            channel=channel, model="model", capability="stream",
        ))

    return run, response, observed


def _probe_chunk(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_stream_capability_probe_consumes_complete_generation_and_closes(monkeypatch, finish) -> None:
    chunks = [
        _probe_chunk({"role": "assistant"}),
        _probe_chunk({"content": "OK"}),
        _probe_chunk(finish=finish),
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    run, response, observed = _mock_stream_probe(monkeypatch, chunks)

    assert run() is None
    assert observed == chunks
    assert response.close_count == 1


@pytest.mark.parametrize("chunks,error", [
    ([], UpstreamProtocolError),
    ([_probe_chunk({"role": "assistant"}), _probe_chunk(finish="stop")], RuntimeError),
    ([_probe_chunk(finish="stop")], RuntimeError),
    ([_probe_chunk({"content": ""}), _probe_chunk(finish="stop")], RuntimeError),
    ([_probe_chunk({"content": "OK"})], UpstreamProtocolError),
    ([_probe_chunk({"content": "OK"}), _probe_chunk({"content": "late"}, finish="")], UpstreamProtocolError),
])
def test_stream_capability_probe_rejects_empty_or_unfinished_generation(monkeypatch, chunks, error) -> None:
    run, response, observed = _mock_stream_probe(monkeypatch, chunks)

    with pytest.raises(error):
        run()

    assert observed == chunks
    assert response.close_count == 1


@pytest.mark.parametrize("chunks", [
    [_probe_chunk({"role": "assistant"})],
    [_probe_chunk({"content": "OK"})],
    [_probe_chunk({"content": "OK"}), _probe_chunk(finish="stop")],
])
def test_stream_capability_probe_rejects_late_transport_errors_and_closes(monkeypatch, chunks) -> None:
    error = RuntimeError("upstream disconnected before completion")
    run, response, observed = _mock_stream_probe(monkeypatch, chunks, error=error)

    with pytest.raises(RuntimeError, match="upstream disconnected before completion"):
        run()

    assert observed == chunks
    assert response.close_count == 1


def test_stream_capability_probe_rejects_late_error_payload_and_closes(monkeypatch) -> None:
    chunks = [
        _probe_chunk({"content": "OK"}), _probe_chunk(finish="stop"),
        {"error": {"message": "late provider failure"}},
    ]
    run, response, observed = _mock_stream_probe(monkeypatch, chunks)

    with pytest.raises(UpstreamProtocolError, match="late provider failure"):
        run()

    assert observed == chunks
    assert response.close_count == 1


@pytest.mark.parametrize("protocol", ["openai", "openai_responses", "anthropic"])
@pytest.mark.parametrize("complete", [True, False])
def test_stream_capability_probe_checks_real_adapter_completion(monkeypatch, protocol, complete) -> None:
    if protocol == "openai":
        events = [_probe_chunk({"content": "OK"}), _probe_chunk(finish="stop")]
    elif protocol == "openai_responses":
        events = [
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed", "response": {
                "id": "resp-probe", "object": "response", "status": "completed",
                "model": "model", "output": [],
            }},
        ]
    else:
        events = [
            {"type": "message_start", "message": {
                "id": "msg-probe", "type": "message", "role": "assistant", "model": "model",
                "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 0},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    if not complete:
        events = events[:-1]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    responses = []

    def upstream(request):
        response = Response(200, content=body, headers={"content-type": "text/event-stream"})
        responses.append(response)
        return response

    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        transport=MockTransport(upstream), **kwargs,
    ))
    channel = Channel(
        id=1, name="mock", type="openai", protocol=protocol, key="unused",
        base_url="https://mock.invalid/v1", models=["model"], model_mapping={}, extra={},
    )

    def run():
        return asyncio.run(channels_api._probe_generation_capability(
            channel=channel, model="model", capability="stream",
        ))

    if complete:
        assert run() is None
    else:
        with pytest.raises(UpstreamProtocolError):
            run()
    assert len(responses) == 1
    assert responses[0].is_closed
