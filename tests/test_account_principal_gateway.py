import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials
import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from rotor import database
from rotor.api.v1 import anthropic, anthropic_models, chat, embeddings, images, models, responses
from rotor.application_settings import ApplicationSettings, application_settings
from rotor.config import settings
from rotor.conversations import store as store_module
from rotor.conversations.store import ConversationStore
from rotor.core.deps import get_current_token, get_optional_token
from rotor.core.exceptions import APIRouterException, api_router_exception_handler
from rotor.core.user_access import resolve_account_principal
from rotor.database import Base, create_database_engine, get_db
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.conversation import ConversationRecord
from rotor.models.log import RequestLog
from rotor.models.response_route import ResponseRoute
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLease, SessionLeaseEvent
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/anthropic/v1/messages"}
ACCOUNT_HEADERS = {"X-Test-Account": "1", "X-Rotor-Session-Id": "shared-session"}
KEY_HEADERS = {"Authorization": "Bearer sk-owned-api-key", "X-Rotor-Session-Id": "shared-session"}


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, image=False):
        self.image = image

    async def __aiter__(self):
        if self.image:
            yield b'event: image_generation.completed\ndata: {"type":"image_generation.completed","usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}\n\n'
            return
        chunk = {"id": "chatcmpl-account", "object": "chat.completion.chunk", "created": 123, "model": "model-a"}
        for choices, usage in [
            ([{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}], None),
            ([{"index": 0, "delta": {}, "finish_reason": "stop"}], None),
            ([], {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}),
        ]:
            payload = {**chunk, "choices": choices}
            if usage is not None:
                payload["usage"] = usage
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"


@asynccontextmanager
async def gateway(tmp_path, monkeypatch, *, native_responses=False):
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            AdminUser(id=1, username="alice", password_hash="unused", enabled=True,
                      must_change_password=False, quota=1000, allowed_channels=[1], allowed_models=["model-a"]),
            AdminUser(id=2, username="bob", password_hash="unused", enabled=True,
                      must_change_password=False, quota=1000, allowed_channels=[2], allowed_models=["model-a"]),
            Channel(id=1, name="first", type="openai", protocol="openai_responses" if native_responses else "openai",
                    key="first-key", base_url="https://first.invalid/v1", models=["model-a", "hidden-model"]),
            Channel(id=2, name="second", type="openai", protocol="openai_responses" if native_responses else "openai",
                    key="second-key", base_url="https://second.invalid/v1", models=["model-a", "channel-secret"]),
        ])
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Token)) == 0

    monkeypatch.setattr(settings, "CONVERSATION_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "CONVERSATION_STORE_DIR", str(tmp_path / "conversations"))
    monkeypatch.setattr(database, "async_session_maker", sessions)
    monkeypatch.setattr(store_module, "async_session_maker", sessions)
    monkeypatch.setattr(application_settings, "get", lambda: ApplicationSettings())
    store = ConversationStore()
    store.attach()
    sent = []

    def upstream(request):
        sent.append(request)
        host = request.url.host
        assert host in {"first.invalid", "second.invalid"}
        assert request.headers["authorization"] == f"Bearer {host.split('.')[0]}-key"
        if request.method == "GET" or native_responses:
            return httpx.Response(200, json={
                "id": "resp_shared", "object": "response", "created_at": 123,
                "model": "model-a", "status": "completed",
                "output": [{"id": "msg_shared", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": host, "annotations": []}]}],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            })
        body = json.loads(request.content)
        assert body["model"] == "model-a"
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"object": "list", "model": "model-a",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.25, 0.5]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3}})
        image = request.url.path.endswith("/images/generations")
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ProviderStream(image=image))
        if image:
            return httpx.Response(200, json={"created": 123, "data": [{"b64_json": "aGVsbG8="}],
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}})
        return httpx.Response(200, json={
            "id": "chatcmpl-account", "object": "chat.completion", "created": 123, "model": "model-a",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    routing = RoutingEngine("fallback_order")
    routing.protocol_affinity_enabled = False
    for module in (chat, responses, anthropic, images, embeddings):
        if hasattr(module, "async_session_maker"):
            monkeypatch.setattr(module, "async_session_maker", sessions)
        if hasattr(module, "conversation_store"):
            monkeypatch.setattr(module, "conversation_store", store)
        monkeypatch.setattr(module, "routing_engine", routing)
        monkeypatch.setattr(module, "accounting_service", AccountingService())
        monkeypatch.setattr(module, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

    app = FastAPI()
    app.add_exception_handler(APIRouterException, api_router_exception_handler)
    for module in (chat, responses, models, images, embeddings):
        app.include_router(module.router, prefix="/v1")
    for module in (anthropic, anthropic_models):
        app.include_router(module.router, prefix="/anthropic/v1")

    async def db_dependency():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def admitted_principal(request: Request, db=Depends(get_db)):
        # Phase one tests an admitted account; browser/OAuth entry points stay unchanged.
        account_id = request.headers.get("X-Test-Account")
        if account_id is not None:
            return await resolve_account_principal(db, int(account_id))
        authorization = request.headers.get("Authorization", "")
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=authorization.removeprefix("Bearer "))
        return await get_current_token(credentials=credentials, x_api_key=None, db=db)

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_current_token] = admitted_principal
    app.dependency_overrides[get_optional_token] = admitted_principal
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(client=client, sessions=sessions, sent=sent, store=store, routing=routing)
    finally:
        await store.shutdown()
        await engine.dispose()


async def infer(env, entrypoint="chat", *, stream=False, model="model-a", headers=ACCOUNT_HEADERS, **fields):
    payload = {"model": model, "stream": stream, **fields}
    if entrypoint == "responses":
        payload.setdefault("input", "hello")
        payload.setdefault("store", False)
    else:
        payload["messages"] = [{"role": "user", "content": "hello"}]
        if entrypoint == "anthropic":
            payload["max_tokens"] = 20
    return await env.client.post(PATHS[entrypoint], json=payload, headers=headers)


@pytest.mark.parametrize("entrypoint", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_account_without_any_key_supports_protocols_and_owned_usage(tmp_path, monkeypatch, entrypoint, stream):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            response = await infer(env, entrypoint, stream=stream)
            assert response.status_code == 200, response.text
            assert "hello" in response.text
            if stream:
                assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[entrypoint] in response.text
                assert "stream_error" not in response.text and "response.failed" not in response.text
            await env.store.drain()
            async with env.sessions() as db:
                assert await db.scalar(select(func.count()).select_from(Token)) == 0
                assert (await db.get(AdminUser, 1)).used_quota == 6
                assert (await db.get(AdminUser, 2)).used_quota == 0
                ledger = (await db.scalars(select(UsageLedger))).one()
                log = (await db.scalars(select(RequestLog))).one()
                conversation = (await db.scalars(select(ConversationRecord))).one()
                decision = (await db.scalars(select(RoutingDecisionRecord))).one()
                lease = (await db.scalars(select(SessionLease))).one()
                event = (await db.scalars(select(SessionLeaseEvent))).one()
                for record in (ledger, log, conversation, lease, event):
                    assert (record.owner_id, record.token_id, record.channel_id) == (1, None, 1)
                assert ledger.total_tokens == 6 and ledger.usage_source == "provider" and log.success
                assert ledger.user_id is None and conversation.user_id is None
                assert decision.token_id is None and decision.feature_snapshot["owner_id"] == 1
                assert conversation.request_id == ledger.request_id == decision.request_id
                assert env.store.dropped_retry_exhausted == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("entrypoint", ["images", "image_stream", "embeddings"])
def test_account_identity_supports_other_inference_routes(tmp_path, monkeypatch, entrypoint):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            if entrypoint == "embeddings":
                response = await env.client.post("/v1/embeddings", json={"model": "model-a", "input": "hello"}, headers=ACCOUNT_HEADERS)
                expected_usage = 3
            else:
                response = await env.client.post("/v1/images/generations", json={"model": "model-a", "prompt": "house", "size": "1024x1024", "stream": entrypoint == "image_stream"}, headers=ACCOUNT_HEADERS)
                expected_usage = 5
            assert response.status_code == 200, response.text
            assert "stream_error" not in response.text
            async with env.sessions() as db:
                assert await db.scalar(select(func.count()).select_from(Token)) == 0
                assert (await db.get(AdminUser, 1)).used_quota == expected_usage
                ledger = (await db.scalars(select(UsageLedger))).one()
                log = (await db.scalars(select(RequestLog))).one()
                assert ledger.total_tokens == expected_usage
                assert (ledger.owner_id, ledger.token_id) == (1, None)
                assert (log.owner_id, log.token_id) == (1, None)
    asyncio.run(exercise())


@pytest.mark.parametrize("restriction", ["channels", "models", "disabled", "deleted", "password", "quota"])
def test_account_permissions_and_quota_are_checked_live_without_key(tmp_path, monkeypatch, restriction):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            discovery = await env.client.get("/v1/models", headers=ACCOUNT_HEADERS)
            assert [item["id"] for item in discovery.json()["data"]] == ["model-a"]
            values = {"channels": {"allowed_channels": []}, "models": {"allowed_models": []},
                      "disabled": {"enabled": False}, "deleted": {}, "password": {"must_change_password": True},
                      "quota": {"quota": 0}}[restriction]
            async with env.sessions() as db:
                if restriction == "deleted":
                    await db.execute(delete(AdminUser).where(AdminUser.id == 1))
                else:
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(**values))
                await db.commit()
            for entrypoint in PATHS:
                rejected = await infer(env, entrypoint)
                assert rejected.status_code == {"channels": 404, "models": 404, "disabled": 403, "deleted": 403, "password": 403, "quota": 429}[restriction], rejected.text
            assert env.sent == []
            discovery = await env.client.get("/v1/models", headers=ACCOUNT_HEADERS)
            if restriction in {"channels", "models"}:
                assert discovery.status_code == 200 and discovery.json()["data"] == []
            else:
                assert discovery.status_code == (429 if restriction == "quota" else 403)
            if restriction == "quota":
                async with env.sessions() as db:
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(quota=1000))
                    await db.commit()
                assert (await infer(env)).status_code == 200
    asyncio.run(exercise())


def test_api_key_and_account_share_only_account_counter_with_key_limits_unchanged(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                db.add(Token(id=11, key="sk-owned-api-key", owner_id=1, name="explicit key", quota=6,
                             allowed_channels=[1], allowed_models=["model-a"]))
                await db.commit()
            assert (await infer(env)).status_code == 200
            assert (await infer(env, headers=KEY_HEADERS)).status_code == 200
            async with env.sessions() as db:
                owner, key = await db.get(AdminUser, 1), await db.get(Token, 11)
                assert owner.used_quota == 12
                assert (key.used_quota, key.token_count, key.request_count, key.enabled) == (6, 6, 1, False)
                ledger = (await db.scalars(select(UsageLedger).order_by(UsageLedger.id))).all()
                assert [(row.owner_id, row.token_id) for row in ledger] == [(1, None), (1, 11)]
                leases = (await db.scalars(select(SessionLease))).all()
                assert {(row.owner_id, row.token_id) for row in leases} == {(1, None), (None, 11)}
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(quota=12))
                await db.commit()
            assert (await infer(env)).status_code == 429
            assert (await infer(env, headers=KEY_HEADERS)).status_code == 401
            async with env.sessions() as db:
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(quota=1000))
                await db.commit()
            assert (await infer(env)).status_code == 200
            assert (await infer(env, headers=KEY_HEADERS)).status_code == 401
    asyncio.run(exercise())


def test_native_response_ownership_and_continuation_are_isolated_between_accounts(tmp_path, monkeypatch):
    async def exercise():
        async with gateway(tmp_path, monkeypatch, native_responses=True) as env:
            bob = {**ACCOUNT_HEADERS, "X-Test-Account": "2"}
            first = await infer(env, "responses", store=True)
            assert first.status_code == 200, first.text
            assert "first.invalid" in first.text
            calls = len(env.sent)
            assert (await env.client.get("/v1/responses/resp_shared", headers=bob)).status_code == 404
            assert (await infer(env, "responses", headers=bob, store=True, previous_response_id="resp_shared")).status_code == 404
            assert len(env.sent) == calls
            second = await infer(env, "responses", headers=bob, store=True)
            assert second.status_code == 200 and "second.invalid" in second.text
            for headers, host in [(ACCOUNT_HEADERS, "first.invalid"), (bob, "second.invalid")]:
                fetched = await env.client.get("/v1/responses/resp_shared", headers=headers)
                assert fetched.status_code == 200 and host in fetched.text
                continuation = await infer(env, "responses", headers=headers, store=True, previous_response_id="resp_shared")
                assert continuation.status_code == 200 and host in continuation.text
                assert json.loads(env.sent[-1].content)["previous_response_id"] == "resp_shared"
            async with env.sessions() as db:
                routes = (await db.scalars(select(ResponseRoute))).all()
                assert {(row.owner_id, row.token_id, row.channel_id, row.response_id) for row in routes} == {(1, None, 1, "resp_shared"), (2, None, 2, "resp_shared")}
                leases = (await db.scalars(select(SessionLease))).all()
                assert {(row.owner_id, row.token_id, row.channel_id, row.session_id) for row in leases} == {(1, None, 1, "shared-session"), (2, None, 2, "shared-session")}
                assert (await db.get(AdminUser, 1)).used_quota == (await db.get(AdminUser, 2)).used_quota == 12
                assert await db.scalar(select(func.count()).select_from(Token)) == 0
    asyncio.run(exercise())
