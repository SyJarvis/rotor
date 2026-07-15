"""Tests for the async ConversationStore (request-level JSONL archive).

Covers the request-path handle-accumulation behaviour, the background worker's
single-line-per-request file output + DB upsert/update, idempotency of start
(upsert) and finish (committed guard), failed-request recording, tool-field
preservation, sanitization, backpressure (drop on full queue), graceful
shutdown drain, and the retry path for transient failures.

The project's existing async tests call ``asyncio.run`` directly inside sync
``def test_*`` — no pytest-asyncio config exists. We follow that convention.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def modules(tmp_path, monkeypatch):
    """Reload config + store with per-test env overrides."""
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir()
    monkeypatch.setenv("CONVERSATION_STORE_DIR", str(conv_dir))
    monkeypatch.setenv("CONVERSATION_STORE_ENABLED", "true")
    monkeypatch.setenv("SAVE_CONVERSATION_BODY", "true")
    monkeypatch.setenv("SAVE_PROVIDER_RESPONSE", "true")
    monkeypatch.setenv("CONVERSATION_QUEUE_MAXSIZE", "1000")

    import rotor.config as cfg

    cfg.get_settings.cache_clear()
    monkeypatch.setattr(cfg, "settings", cfg.get_settings())

    import rotor.database as db_mod
    import rotor.conversations.store as store_mod

    monkeypatch.setattr(store_mod, "settings", cfg.settings, raising=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    from rotor.models.conversation import ConversationRecord  # noqa: F401
    from rotor.models.token import Token  # noqa: F401
    from rotor.models.channel import Channel  # noqa: F401
    from rotor.models.usage import UsageLedger  # noqa: F401

    from rotor.database import Base as _ProjectBase

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(_ProjectBase.metadata.create_all)

    asyncio.run(_init())

    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "async_session_maker", session_maker, raising=True)
    monkeypatch.setattr(store_mod, "async_session_maker", session_maker, raising=True)

    return {
        "store_mod": store_mod,
        "session_maker": session_maker,
        "conv_dir": conv_dir,
        "settings": cfg.settings,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_token() -> SimpleNamespace:
    return SimpleNamespace(id=42, user_id="alice")


def _fake_channel() -> SimpleNamespace:
    return SimpleNamespace(id=7, type="zhipu", protocol="openai_chat")


def _make_request(messages=None):
    from rotor.schemas.request import ChatCompletionRequest, ChatMessage, Role

    if messages is None:
        messages = [ChatMessage(role=Role.USER, content="hello")]
    return ChatCompletionRequest(model="glm-4", messages=messages)


async def _drain(store, timeout: float = 1.0):
    """Wait until the worker has consumed everything currently in the queue."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        q = store._queue
        if q is None or q.empty():
            await asyncio.sleep(0.01)
            if q is None or q.empty():
                return
        await asyncio.sleep(0.01)


def _read_lines(conv_dir):
    files = list(Path(conv_dir).rglob("*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {len(files)}: {files}"
    return [json.loads(l) for l in files[0].read_text().splitlines() if l]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_lifecycle_writes_one_line_and_db_row(modules):
    store_mod = modules["store_mod"]
    session_maker = modules["session_maker"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            handle = await store.start(
                None,
                conversation_id="conv-1",
                request_id="req-1",
                token=_fake_token(),
                request=_make_request(),
                protocol="openai_chat",
            )
            await store.append_routing(handle, _fake_channel())
            await store.append_response(
                handle,
                {"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
            )
            await store.append_usage(
                handle, {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
            )
            await store.finish(handle, "success", 123)

            await _drain(store)

            rows = _read_lines(conv_dir)
            assert len(rows) == 1
            row = rows[0]
            assert row["conversation_id"] == "conv-1"
            assert row["request_id"] == "req-1"
            assert row["model"] == "glm-4"
            assert row["protocol"] == "openai_chat"
            assert row["provider"] == "zhipu"
            assert row["status"] == "success"
            assert row["latency_ms"] == 123
            assert row["messages"] == [
                {"role": "user", "content": "hello", "name": None,
                 "tool_calls": None, "tool_call_id": None}
            ]
            assert row["response"] == {"role": "assistant", "content": "hi"}
            assert row["usage"]["total_tokens"] == 8
            assert row["error"] is None

            from rotor.models.conversation import ConversationRecord

            async with session_maker() as s:
                db_rows = (await s.execute(select(ConversationRecord))).scalars().all()
                assert len(db_rows) == 1
                r = db_rows[0]
                assert r.status == "success"
                assert r.channel_id == 7
                assert r.provider == "zhipu"
                assert r.model == "glm-4"
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_messages_preserve_tool_fields(modules):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    from rotor.schemas.request import ChatMessage, Role, ToolCall, FunctionCall

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            msgs = [
                ChatMessage(role=Role.USER, content="what's the weather?"),
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=None,
                    tool_calls=[ToolCall(
                        id="call_1", type="function",
                        function=FunctionCall(name="get_weather", arguments='{"q":"sf"}'),
                    )],
                ),
                ChatMessage(role=Role.TOOL, content="sunny", tool_call_id="call_1"),
            ]
            handle = await store.start(
                None, conversation_id="c", request_id="r",
                token=_fake_token(), request=_make_request(msgs), protocol="openai_chat",
            )
            await store.finish(handle, "success", 10)
            await _drain(store)

            row = _read_lines(conv_dir)[0]
            assert row["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"
            assert row["messages"][1]["content"] is None
            assert row["messages"][2]["role"] == "tool"
            assert row["messages"][2]["tool_call_id"] == "call_1"
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_failed_request_is_recorded(modules):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            handle = await store.start(
                None, conversation_id="c", request_id="r",
                token=_fake_token(), request=_make_request(), protocol="openai_chat",
            )
            await store.append_routing(handle, _fake_channel())
            await store.append_error(handle, "HTTPStatusError", "502 bad gateway")
            await store.finish(handle, "failed", 99)

            await _drain(store)

            row = _read_lines(conv_dir)[0]
            assert row["status"] == "failed"
            assert row["response"] is None
            assert row["usage"] is None
            assert row["error"] == {"code": "HTTPStatusError", "message": "502 bad gateway"}
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_finish_is_idempotent(modules):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            handle = await store.start(
                None, conversation_id="c", request_id="r",
                token=_fake_token(), request=_make_request(), protocol="openai_chat",
            )
            await store.finish(handle, "success", 1)
            await store.finish(handle, "success", 1)  # second call is a no-op
            await _drain(store)

            rows = _read_lines(conv_dir)
            assert len(rows) == 1
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_start_upsert_is_idempotent(modules):
    store_mod = modules["store_mod"]
    session_maker = modules["session_maker"]

    from rotor.models.conversation import ConversationRecord

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            for _ in range(3):
                await store.start(
                    None, conversation_id="c", request_id="r",
                    token=_fake_token(), request=_make_request(), protocol="openai_chat",
                )
            await _drain(store)

            async with session_maker() as s:
                db_rows = (await s.execute(select(ConversationRecord))).scalars().all()
                assert len(db_rows) == 1  # upsert keeps a single row
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_provider_response_sanitized(modules):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            handle = await store.start(
                None, conversation_id="c", request_id="r",
                token=_fake_token(), request=_make_request(), protocol="openai_chat",
            )
            # A sensitive key nested inside the assistant message is redacted.
            await store.append_response(handle, {
                "choices": [{"message": {
                    "role": "assistant",
                    "content": "ok",
                    "token": "sk-secret",
                }}],
            })
            await store.finish(handle, "success", 1)
            await _drain(store)

            row = _read_lines(conv_dir)[0]
            assert row["response"]["token"] == "***"
            assert row["response"]["content"] == "ok"
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_disabled_store_is_noop(modules, monkeypatch):
    monkeypatch.setenv("CONVERSATION_STORE_ENABLED", "false")
    import rotor.config as cfg

    cfg.get_settings.cache_clear()
    monkeypatch.setattr(cfg, "settings", cfg.get_settings())
    store_mod = modules["store_mod"]
    monkeypatch.setattr(store_mod, "settings", cfg.settings, raising=True)

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        assert store._worker_task is None
        assert store._queue is None

        handle = await store.start(
            None, conversation_id="conv-x", request_id="req-x",
            token=_fake_token(), request=_make_request(), protocol="openai_chat",
        )
        await store.append_routing(handle, _fake_channel())
        await store.finish(handle, "success", 1)

        await asyncio.sleep(0.05)
        assert list(Path(modules["conv_dir"]).rglob("*.jsonl")) == []

    asyncio.run(scenario())


def test_queue_full_drops_commit_without_blocking(modules, monkeypatch):
    store_mod = modules["store_mod"]
    monkeypatch.setattr(modules["settings"], "CONVERSATION_QUEUE_MAXSIZE", 1)

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()
        try:
            store._queue.put_nowait(store_mod._Event(kind="db_only"))  # fill to capacity
            assert store._queue.qsize() == 1

            handle = store_mod.ConversationHandle(
                conversation_id="c", request_id="r",
                file_path=Path(modules["conv_dir"]) / "x.jsonl",
            )
            # finish enqueues a commit; queue is full → dropped, must not block.
            await asyncio.wait_for(store.finish(handle, "success", 1), timeout=1.0)
            assert store._queue.qsize() == 1
        finally:
            await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_shutdown_drains_pending_commit(modules, monkeypatch):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()

        for i in range(3):
            handle = await store.start(
                None, conversation_id=f"c{i}", request_id=f"r{i}",
                token=_fake_token(), request=_make_request(), protocol="openai_chat",
            )
            await store.finish(handle, "success", i)

        # shutdown should drain the 3 pending commits before exiting.
        await store.shutdown(timeout=5)

        rows = _read_lines(conv_dir)
        assert len(rows) == 3

    asyncio.run(scenario())


def test_commit_retried_then_succeeds(modules, monkeypatch, caplog):
    store_mod = modules["store_mod"]
    conv_dir = modules["conv_dir"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()

        real_append = store_mod.ConversationStore._append_raw
        calls = {"n": 0}

        async def flaky_append(self, file_path, line):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("simulated disk hiccup")
            await real_append(self, file_path, line)

        monkeypatch.setattr(store_mod.ConversationStore, "_append_raw", flaky_append)

        handle = await store.start(
            None, conversation_id="c", request_id="r",
            token=_fake_token(), request=_make_request(), protocol="openai_chat",
        )
        with caplog.at_level(logging.ERROR):
            await store.finish(handle, "success", 5)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if calls["n"] >= 3:
                    break

        rows = _read_lines(conv_dir)
        assert len(rows) == 1
        assert not any("Dropping event" in r.message for r in caplog.records)

        await store.shutdown(timeout=2)

    asyncio.run(scenario())


def test_persistently_failing_commit_is_dropped(modules, monkeypatch, caplog):
    store_mod = modules["store_mod"]

    async def scenario():
        store = store_mod.ConversationStore()
        store.attach()

        async def always_fail(self, file_path, line):
            raise RuntimeError("persistent failure")

        monkeypatch.setattr(store_mod.ConversationStore, "_append_raw", always_fail)

        handle = await store.start(
            None, conversation_id="c", request_id="r",
            token=_fake_token(), request=_make_request(), protocol="openai_chat",
        )
        with caplog.at_level(logging.ERROR):
            await store.finish(handle, "success", 5)
            for _ in range(40):
                await asyncio.sleep(0.02)

        assert any(
            "Dropping event" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

        await store.shutdown(timeout=2)

    asyncio.run(scenario())
