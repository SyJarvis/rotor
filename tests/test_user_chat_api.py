import asyncio
from contextlib import asynccontextmanager
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient, Response as HTTPResponse
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api import chat
from rotor.api.admin import chat as engine
from rotor.core import admin_auth, user_access
from rotor.database import Base, get_db
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.request_attempt import RequestAttempt
from rotor.models.token import Token
from rotor.models.usage import UsageLedger
from rotor.services.user_chat_store import ChatStore
from tests.test_admin_chat import attachment, chat_sse, docx_bytes, responses_sse


@asynccontextmanager
async def fixture(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            AdminUser(id=1, username="alice", password_hash="test", enabled=True, must_change_password=False,
                      role="admin", quota=1000, allowed_channels=[1], default_token_id=11),
            AdminUser(id=2, username="bob", password_hash="test", enabled=True, must_change_password=False,
                      role="user", quota=1000, allowed_channels=[2], default_token_id=21),
            Token(id=11, owner_id=1, name="base", key="alice-default", enabled=True, allowed_channels=None),
            Token(id=12, owner_id=1, name="other", key="alice-other", enabled=True, allowed_channels=None),
            Token(id=21, owner_id=2, name="base", key="bob-default", enabled=True, allowed_channels=None),
            Token(id=99, name="legacy", key="unowned-legacy", enabled=True, allowed_channels=None),
            Channel(id=1, name="first", type="openai", key="channel-key", base_url="https://invalid.test", models=["model-a"]),
            Channel(id=2, name="second", type="openai", key="channel-key-2", base_url="https://invalid.test", models=["model-b"]),
        ])
        await db.commit()
    store = ChatStore(tmp_path / "chats")
    monkeypatch.setattr(chat, "_store", lambda: store)
    monkeypatch.setattr(engine, "async_session_maker", sessions)
    app = FastAPI()

    async def actor(request: Request):
        user_id = int(request.headers.get("x-test-user", "1"))
        return admin_auth.AdminActor(f"user-{user_id}", "test", "session", False, user_id, 1, role="admin" if user_id == 1 else "user")

    async def db_dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[admin_auth.require_admin_actor] = actor
    app.dependency_overrides[get_db] = db_dependency
    app.include_router(chat.router, prefix="/api")
    app.include_router(engine.router, prefix="/api/admin")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, store, sessions
    finally:
        await database.dispose()


async def create(client, *, user=1):
    response = await client.post("/api/chat/conversations", json={"settings": {"model": "model-a" if user == 1 else "model-b"}}, headers={"x-test-user": str(user)})
    assert response.status_code == 201
    return response.json()


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def completion(document, content="hello", **kwargs):
    return {"conversation_id": document["id"], "expected_revision": document["revision"],
            "settings": document["settings"], "message": {"content": content}, **kwargs}


def test_crud_and_attachment_api_are_isolated_and_reject_history_overrides(tmp_path, monkeypatch):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, _):
            first, second = await create(client), await create(client, user=2)
            path = f"/api/chat/conversations/{first['id']}"
            assert (await client.get(path, headers={"x-test-user": "2"})).status_code == 404
            assert [item["id"] for item in (await client.get("/api/chat/conversations", headers={"x-test-user": "2"})).json()] == [second["id"]]
            assert (await client.patch(path, json={"expected_revision": 1, "messages": [{"role": "assistant", "content": "forged"}]})).status_code == 422
            uploaded = await client.post(path + "/attachments", json={"kind": "file", "name": "notes.txt", "mime_type": "text/plain", "size": 5, "content": "facts"})
            assert uploaded.status_code == 201
            metadata = uploaded.json()
            assert metadata["mimeType"] == "text/plain" and "content" not in metadata
            attachment_path = path + "/attachments/" + metadata["id"]
            assert (await client.get(attachment_path)).json()["content"] == "facts"
            assert (await client.get(attachment_path, headers={"x-test-user": "2"})).status_code == 404
            patched = await client.patch(path, json={"expected_revision": 1, "draft": {"text": "next", "attachments": [metadata]}})
            assert patched.status_code == 200
            assert (await client.delete(attachment_path)).status_code == 409
            assert (await client.patch(path, json={"expected_revision": 1, "title": "stale"})).status_code == 409
            assert (await client.post("/api/chat/completions", json=completion(patched.json(), messages=[]))).status_code == 422
            deleted = await client.delete(path, params={"expected_revision": patched.json()["revision"]})
            assert deleted.status_code == 204 and not (store.root / "1" / first["id"]).exists()
            empty = await client.post("/api/chat/conversations", json={"settings": {"model": ""}})
            assert empty.status_code == 201 and empty.json()["draft"] == {"text": "", "attachments": []}
            assert (await client.get("/api/admin/chat/models", headers={"x-test-user": "2"})).status_code == 403

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["image", "docx", "text"])
def test_attachment_validation_and_preview_reuse_existing_formats(tmp_path, monkeypatch, kind):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, _, _):
            document = await create(client)
            path = f"/api/chat/conversations/{document['id']}/attachments"
            payload = (attachment() if kind == "image" else attachment(docx_bytes(), mime=engine._DOCX_MIME, kind="file", name="doc.docx")
                       if kind == "docx" else {"kind": "file", "name": "note.py", "mime_type": "application/octet-stream", "size": 3, "content": "abc"})
            result = await client.post(path, json=payload)
            assert result.status_code == 201
            assert (await client.get(path + "/" + result.json()["id"])).json() == {"id": result.json()["id"], **payload}
            assert (await client.post(path, json={**payload, "size": 1})).status_code == 422
            assert (await client.delete(path + "/" + result.json()["id"])).status_code == 204

    asyncio.run(exercise())


def test_sse_persists_canonical_multiturn_history_and_usage(tmp_path, monkeypatch):
    observed = []

    async def generate(payload, request, principal):
        observed.append((payload, principal))
        yield {"type": "delta", "content": "answer"}
        yield {"type": "done", "answer": "answer", "usage": {"total_tokens": 5}}

    monkeypatch.setattr(engine, "_completion_events", generate)

    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, _):
            document = await create(client)
            response = await client.post("/api/chat/completions", json=completion(document))
            streamed = events(response)
            assert [event["type"] for event in streamed] == ["start", "delta", "done"]
            assert streamed[0]["conversation"]["messages"][-1]["status"] == "running"
            final = streamed[-1]["conversation"]
            assert final == store.get(1, document["id"])
            assert final["messages"][-1]["usage"] == {"total_tokens": 5}
            assert final["title"] == "hello"
            response = await client.post("/api/chat/completions", json=completion(final, "follow up"))
            assert events(response)[-1]["type"] == "done"
            assert [message.content for message in observed[1][0].messages] == ["hello", "answer", "follow up"]
            assert [principal.owner_id for _, principal in observed] == [1, 1]
            assert all(isinstance(principal, user_access.AccountPrincipal) and principal.id is None for _, principal in observed)

    asyncio.run(exercise())


@pytest.mark.parametrize("key_state", ["disabled", "quota", "expired", "restricted", "wrong_owner", "missing_default", "no_tokens"])
def test_web_chat_uses_the_account_independently_of_api_keys(tmp_path, monkeypatch, key_state):
    observed = []

    async def generate(payload, request, principal):
        observed.append(principal)
        yield {"type": "done", "answer": "OK", "usage": {"total_tokens": 0}}

    monkeypatch.setattr(engine, "_completion_events", generate)

    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, sessions):
            document = await create(client)
            async with sessions() as db:
                if key_state == "no_tokens":
                    await db.execute(delete(Token))
                elif key_state in {"wrong_owner", "missing_default"}:
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(default_token_id=21 if key_state == "wrong_owner" else None))
                else:
                    values = {"disabled": {"enabled": False}, "quota": {"quota": 0},
                              "expired": {"expire_time": datetime.now(timezone.utc) - timedelta(days=1)},
                              "restricted": {"allowed_channels": [2], "allowed_models": ["model-b"]}}[key_state]
                    await db.execute(update(Token).where(Token.id == 11).values(**values))
                await db.commit()
            assert (await client.get("/api/chat/models")).json() == {"models": ["model-a"], "account_available": True}
            assert (await client.post("/api/chat/test", json={"model": "model-a"})).status_code == 200
            response = await client.post("/api/chat/completions", json=completion(document))
            assert response.status_code == 200 and events(response)[-1]["type"] == "done"
            assert store.get(1, document["id"])["messages"][-1]["status"] == "done"
            assert (await client.post("/api/admin/chat/completions", json={"model": "model-a", "conversation_id": "legacy", "messages": [{"role": "user", "content": "hi"}]})).status_code == 200
            assert all(isinstance(principal, user_access.AccountPrincipal) and principal.owner_id == 1 and principal.id is None for principal in observed)

    asyncio.run(exercise())


@pytest.mark.parametrize("failure,status", [("account_quota", 429), ("disabled_account", 403), ("password_change", 403), ("restricted_model", 403), ("restricted_channel", 403), ("no_models", 403), ("no_channels", 403), ("missing_account", 403)])
def test_account_access_is_required_before_a_generation_is_persisted(tmp_path, monkeypatch, failure, status):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, sessions):
            document = await create(client)
            async with sessions() as db:
                if failure == "missing_account":
                    await db.execute(delete(AdminUser).where(AdminUser.id == 1))
                else:
                    values = {"account_quota": {"quota": 0}, "disabled_account": {"enabled": False},
                              "password_change": {"must_change_password": True}, "restricted_model": {"allowed_models": ["model-b"]},
                              "restricted_channel": {"allowed_channels": [2]}, "no_models": {"allowed_models": []},
                              "no_channels": {"allowed_channels": []}}[failure]
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(**values))
                await db.commit()
            response = await client.post("/api/chat/completions", json=completion(document))
            assert response.status_code == status
            assert store.get(1, document["id"])["messages"] == []
            assert (await client.post("/api/chat/test", json={"model": "model-a"})).status_code == status
            models = (await client.get("/api/chat/models")).json()
            assert models["models"] == (["model-b"] if failure == "restricted_channel" else [])
            assert models["account_available"] is (failure in {"restricted_model", "restricted_channel", "no_models", "no_channels"})
            assert (await client.post("/api/admin/chat/completions", json={"model": "model-a", "conversation_id": "legacy", "messages": [{"role": "user", "content": "hi"}]})).status_code == status

    asyncio.run(exercise())


def test_models_are_limited_to_current_account(tmp_path, monkeypatch):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, _, _):
            assert (await client.get("/api/chat/models")).json() == {"models": ["model-a"], "account_available": True}
            assert (await client.get("/api/chat/models", headers={"x-test-user": "2"})).json() == {"models": ["model-b"], "account_available": True}
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["close", "cancel", "error"])
def test_stream_checkpoint_disconnect_and_errors_preserve_partial_response_and_next_draft(tmp_path, monkeypatch, mode):
    closed = []

    async def generate(*args):
        try:
            yield {"type": "delta", "content": "partial"}
            if mode == "error":
                raise RuntimeError("Upstream failed")
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(engine, "_completion_events", generate)

    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, _):
            document = await create(client)
            request = Request({"type": "http", "method": "POST", "path": "/api/chat/completions", "headers": []})
            response = await chat.completions(chat.CompletionRequest(**completion(document)), request, SimpleNamespace(user_id=1))
            iterator = response.body_iterator
            start = json.loads((await anext(iterator))[6:])["conversation"]
            assert "partial" in await anext(iterator)
            await asyncio.sleep(0.6)
            checkpoint = store.get(1, document["id"])
            assert checkpoint["messages"][-1]["content"] == "partial"
            store.patch(1, document["id"], start["revision"], {"draft": {"text": "next", "attachments": []}}, expected_draft=start["draftRevision"])
            if mode == "close":
                await iterator.aclose()
            elif mode == "cancel":
                task = asyncio.create_task(anext(iterator))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                event = json.loads((await anext(iterator))[6:])
                assert event["type"] == "error" and event["message"] == "Upstream failed"
                await iterator.aclose()
            final = store.get(1, document["id"])
            assert final["messages"][-1]["status"] == ("error" if mode == "error" else "stopped")
            assert final["messages"][-1]["content"] == "partial" and final["draft"]["text"] == "next"
            assert closed == [True]

    asyncio.run(exercise())


def test_disconnect_before_response_body_starts_releases_generation_lease(tmp_path, monkeypatch):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, _):
            document = await create(client)
            scope = {"type": "http", "method": "POST", "path": "/api/chat/completions", "headers": [], "asgi": {"spec_version": "2.4"}}
            response = await chat.completions(chat.CompletionRequest(**completion(document)), Request(scope), SimpleNamespace(user_id=1))

            async def send(message):
                raise OSError("disconnected")

            async def receive():
                return {"type": "http.disconnect"}

            with pytest.raises(Exception):
                await response(scope, receive, send)
            assert store.get(1, document["id"])["messages"][-1]["status"] == "stopped"

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["begin", "prepare"])
def test_cancel_before_stream_creation_releases_the_lease(tmp_path, monkeypatch, stage):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, _):
            document = await create(client)
            entered = threading.Event()
            release = threading.Event()
            if stage == "begin":
                original = store.begin

                def slow_begin(*args, **kwargs):
                    value = original(*args, **kwargs)
                    entered.set()
                    assert release.wait(3)
                    return value

                monkeypatch.setattr(store, "begin", slow_begin)
            else:
                async def prepare(*args):
                    entered.set()
                    await asyncio.Event().wait()
                monkeypatch.setattr(chat, "_canonical_request", prepare)
            request = Request({"type": "http", "method": "POST", "path": "/api/chat/completions", "headers": []})
            task = asyncio.create_task(chat.completions(chat.CompletionRequest(**completion(document)), request, SimpleNamespace(user_id=1)))
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            final = store.get(1, document["id"])
            assert final["messages"][-1]["status"] == ("stopped" if stage == "begin" else "error")
            store.patch(1, document["id"], final["revision"], {"title": "lease released"})

    asyncio.run(exercise())


@pytest.mark.parametrize("change,fragment", [("disabled", "disabled"), ("quota", "quota"), ("password", "password"), ("model", "not available"), ("channels", "not available"), ("deleted", "disabled")])
def test_account_changes_before_sdk_request_are_rechecked(tmp_path, monkeypatch, change, fragment):
    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, sessions):
            document = await create(client)
            request = Request({"type": "http", "method": "POST", "path": "/api/chat/completions", "headers": []})
            response = await chat.completions(chat.CompletionRequest(**completion(document)), request, SimpleNamespace(user_id=1))
            async with sessions() as db:
                if change == "deleted":
                    await db.execute(delete(AdminUser).where(AdminUser.id == 1))
                else:
                    values = {"disabled": {"enabled": False}, "quota": {"quota": 0}, "password": {"must_change_password": True},
                              "model": {"allowed_models": ["model-b"]}, "channels": {"allowed_channels": [2]}}[change]
                    await db.execute(update(AdminUser).where(AdminUser.id == 1).values(**values))
                await db.commit()
            streamed = [json.loads(item[6:]) async for item in response.body_iterator]
            assert streamed[-1]["type"] == "error" and fragment in streamed[-1]["message"]
            assert store.get(1, document["id"])["messages"][-1]["status"] == "error"
            async with sessions() as db:
                assert (await db.get(Token, 12)).used_quota == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("api_format", ["chat", "responses"])
def test_key_changes_after_browser_admission_do_not_affect_the_account(tmp_path, monkeypatch, api_format):
    admitted = []

    async def gateway(data, request, response, db, principal):
        admitted.append(principal)

        async def body():
            for chunk in (chat_sse("OK") if api_format == "chat" else responses_sse("OK")):
                yield chunk

        return engine.StreamingResponse(body())

    monkeypatch.setattr(engine, "chat_completions", gateway)
    monkeypatch.setattr(engine, "create_response", gateway)

    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, sessions):
            document = await create(client)
            document["settings"]["api_format"] = api_format
            request = Request({"type": "http", "method": "POST", "path": "/api/chat/completions", "headers": []})
            response = await chat.completions(chat.CompletionRequest(**completion(document)), request, SimpleNamespace(user_id=1))
            async with sessions() as db:
                await db.execute(update(Token).where(Token.owner_id == 1).values(enabled=False, quota=0))
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(default_token_id=21))
                await db.commit()
            streamed = [json.loads(item[6:]) async for item in response.body_iterator]
            assert streamed[-1]["type"] == "done"
            assert store.get(1, document["id"])["messages"][-1]["content"] == "OK"
            assert len(admitted) == 1 and isinstance(admitted[0], user_access.AccountPrincipal) and admitted[0].owner_id == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("api_format", ["chat", "responses"])
@pytest.mark.parametrize("with_keys", [False, True])
def test_sdk_gateway_stream_charges_only_the_account(tmp_path, monkeypatch, api_format, with_keys):
    import rotor.api.v1.chat as gateway
    import rotor.api.v1.responses as responses_gateway
    from rotor.gateway.routing import RoutingEngine
    from tests.test_chat_attempts import FakeConversationStore

    class Adapter:
        async def make_request(self, request):
            return HTTPResponse(200)

        def map_model_name(self, model):
            return model

        async def stream_convert_response(self, response, request):
            yield {"id": "chatcmpl-user", "object": "chat.completion.chunk", "created": 1, "model": request.model,
                   "choices": [{"index": 0, "delta": {"content": "charged answer"}, "finish_reason": None}]}
            yield {"id": "chatcmpl-user", "object": "chat.completion.chunk", "created": 1, "model": request.model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    async def exercise():
        async with fixture(tmp_path, monkeypatch) as (client, store, sessions):
            async with sessions() as db:
                await db.execute(update(AdminUser).where(AdminUser.id == 1).values(quota=3))
                await db.commit()
            if not with_keys:
                async with sessions() as db:
                    await db.execute(delete(Token))
                    await db.commit()
            routing, archive = RoutingEngine("fallback_order"), FakeConversationStore()
            for module in (gateway, responses_gateway):
                monkeypatch.setattr(module, "async_session_maker", sessions)
                monkeypatch.setattr(module, "conversation_store", archive)
                monkeypatch.setattr(module, "routing_engine", routing)
            monkeypatch.setattr(gateway.AdapterFactory, "create_adapter", lambda *args: Adapter())
            document = await create(client)
            document["settings"]["api_format"] = api_format
            response = await client.post("/api/chat/completions", json=completion(document))
            streamed = events(response)
            assert streamed[-1]["type"] == "done", streamed
            assert streamed[-1]["conversation"]["messages"][-1]["content"] == "charged answer"
            async with sessions() as db:
                assert (await db.get(AdminUser, 1)).used_quota == 5
                assert all(token.used_quota == 0 for token in (await db.scalars(select(Token))).all())
                record = (await db.scalars(select(RequestLog))).one()
                assert record.owner_id == 1 and record.token_id is None and record.total_tokens == 5
                assert (await db.scalars(select(RequestAttempt))).one().request_origin == "user_chat"
            assert store.get(1, document["id"])["messages"][-1]["usage"]["total_tokens"] == 5
            follow_up = await client.post("/api/chat/completions", json=completion(streamed[-1]["conversation"], "another request"))
            assert follow_up.status_code == 429 and follow_up.json()["detail"] == "Account token quota exceeded"
            assert len(store.get(1, document["id"])["messages"]) == 2
            async with sessions() as db:
                ledger = (await db.scalars(select(UsageLedger))).one()
                assert ledger.total_tokens == 5 and ledger.owner_id == 1 and ledger.token_id is None
                assert len((await db.scalars(select(RequestLog))).all()) == 1

    asyncio.run(exercise())
