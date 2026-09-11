import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.api.admin.monitoring as monitoring_api
from rotor.api.admin.monitoring import _today_window, router
from rotor.application_settings import application_settings
from rotor.core.client_session import resolve_client_session
from rotor.database import Base, get_db
from rotor.main import app as rotor_app
from rotor.models.conversation import ConversationRecord
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLease
from rotor.models.usage import UsageLedger


def _routing(
    request_id: str,
    token_id: int,
    source: str | None,
    created_at: datetime,
) -> RoutingDecisionRecord:
    return RoutingDecisionRecord(
        request_id=request_id,
        token_id=token_id,
        model="route-model",
        request_protocol="openai_responses",
        strategy="priority_weighted",
        candidate_channel_ids=[1],
        required_capabilities=[],
        feature_snapshot={"session_source": source},
        created_at=created_at,
    )


def _usage(
    request_id: str,
    token_id: int,
    session_id: str,
    model: str,
    total_tokens: int,
    created_at: datetime,
) -> UsageLedger:
    return UsageLedger(
        request_id=request_id,
        conversation_id=session_id,
        token_id=token_id,
        model=model,
        request_protocol="openai_responses",
        total_tokens=total_tokens,
        created_at=created_at,
    )


async def _monitoring_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(router, prefix="/api/admin")

    async def override_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )
    return engine, sessions, client


def test_today_window_uses_display_timezone_midnight() -> None:
    generated_at = datetime(2026, 9, 1, 17, 30, tzinfo=timezone.utc)

    selected, start, end = _today_window(generated_at, "Asia/Shanghai")

    assert selected.isoformat() == "2026-09-02"
    assert start == datetime(2026, 9, 1, 16)
    assert end == datetime(2026, 9, 2, 16)


def test_monitoring_filters_by_configured_display_timezone(monkeypatch) -> None:
    async def scenario() -> None:
        engine, sessions, client = await _monitoring_client()
        generated_at = datetime(2026, 9, 1, 17, 30, tzinfo=timezone.utc)
        timezone_name = "Asia/Shanghai"
        selected, start, end = _today_window(generated_at, timezone_name)
        monkeypatch.setattr(monitoring_api, "_utc_now", lambda: generated_at)
        monkeypatch.setattr(
            monitoring_api.application_settings,
            "get",
            lambda: SimpleNamespace(display_timezone=timezone_name),
        )
        try:
            async with sessions() as db:
                db.add_all([
                    _routing(
                        "before",
                        1,
                        "codex-session-id",
                        start - timedelta(microseconds=1),
                    ),
                    _routing("at-start", 1, "codex-session-id", start),
                    _routing(
                        "before-end",
                        1,
                        "codex-session-id",
                        end - timedelta(microseconds=1),
                    ),
                    _routing("at-end", 1, "codex-session-id", end),
                    _usage(
                        "before",
                        1,
                        "outside-before",
                        "gpt",
                        100,
                        start - timedelta(microseconds=1),
                    ),
                    _usage(
                        "at-start",
                        1,
                        "inside",
                        "gpt-a",
                        10,
                        start,
                    ),
                    _usage(
                        "before-end",
                        1,
                        "inside",
                        "gpt-b",
                        20,
                        end - timedelta(microseconds=1),
                    ),
                    _usage(
                        "at-end",
                        1,
                        "outside-after",
                        "gpt",
                        100,
                        end,
                    ),
                ])
                await db.commit()

            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            payload = response.json()
            assert payload["date"] == selected.isoformat()
            assert payload["timezone"] == timezone_name
            assert payload["generated_at"] == generated_at.isoformat().replace(
                "+00:00",
                "Z",
            )
            codex = payload["agents"][0]
            assert codex["session_count"] == 1
            assert codex["turn_count"] == 2
            assert codex["total_tokens"] == 30
            assert codex["sessions"][0]["session_id"] == "inside"
            assert codex["sessions"][0]["models"] == ["gpt-a", "gpt-b"]
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())


def test_monitoring_groups_stable_sources_by_tenant_session_and_lease() -> None:
    async def scenario() -> None:
        engine, sessions, client = await _monitoring_client()
        generated_at = datetime.now(timezone.utc)
        _, start, end = _today_window(
            generated_at,
            application_settings.get().display_timezone,
        )
        now = start + (end - start) / 2
        try:
            async with sessions() as db:
                db.add_all([
                    _routing("codex-a", 1, "codex-session-id", now),
                    _routing("codex-a-extra", 1, "codex-session-id", now + timedelta(seconds=1)),
                    _routing("codex-b", 2, "codex-session-id", now + timedelta(seconds=2)),
                    _routing("claude-cross", 1, "claude-code-session-id", now + timedelta(seconds=3)),
                    _routing("claude-a", 3, "claude-code-session-id", now),
                    _routing("claude-b", 3, "metadata-user-session-id", now + timedelta(seconds=1)),
                    _routing("open-a", 4, "opencode-session-affinity", now),
                    _routing("pi-a", 7, "pi-prompt-cache-key", now),
                    _routing("grok-a", 8, "grok-prompt-cache-key", now),
                    _routing("generic", 5, "x-conversation-id", now),
                    _routing("missing", 6, None, now),
                    _usage("codex-a", 1, "shared", "gpt-a", 10, now),
                    _usage("codex-a", 1, "shared", "gpt-a", 5, now + timedelta(seconds=1)),
                    _usage("codex-a-extra", 1, "shared", "gpt-b", 7, now + timedelta(seconds=2)),
                    _usage("codex-b", 2, "shared", "gpt-c", 20, now + timedelta(seconds=3)),
                    _usage("claude-cross", 1, "shared", "haiku", 11, now + timedelta(seconds=3)),
                    _usage("claude-a", 3, "claude", "sonnet", 30, now),
                    _usage("claude-b", 3, "claude", "opus", 40, now + timedelta(seconds=1)),
                    _usage("open-a", 4, "open", "qwen", 50, now),
                    _usage("pi-a", 7, "pi-session", "gpt-pi", 12, now),
                    _usage("grok-a", 8, "grok-conversation", "grok-model", 14, now),
                    _usage("generic", 5, "conv_generated", "other", 1000, now),
                    _usage("missing", 6, "conv_missing", "other", 1000, now),
                    SessionLease(
                        token_id=1,
                        session_id="shared",
                        logical_model="gpt-a",
                        channel_id=1,
                        last_used_at=now,
                        expires_at=generated_at.replace(tzinfo=None) + timedelta(hours=1),
                    ),
                    SessionLease(
                        token_id=1,
                        session_id="shared",
                        logical_model="gpt-old",
                        channel_id=1,
                        last_used_at=now,
                        expires_at=generated_at.replace(tzinfo=None) - timedelta(seconds=1),
                    ),
                    SessionLease(
                        token_id=2,
                        session_id="shared",
                        logical_model="gpt-c",
                        channel_id=1,
                        last_used_at=now,
                        expires_at=generated_at.replace(tzinfo=None) - timedelta(seconds=1),
                    ),
                ])
                await db.commit()

            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            payload = response.json()
            assert [agent["id"] for agent in payload["agents"]] == [
                "codex", "claude_code", "opencode", "pi", "grok_build", "mindcode",
            ]

            codex = payload["agents"][0]
            assert codex["session_count"] == 2
            assert codex["active_session_count"] == 1
            assert codex["turn_count"] == 3
            assert codex["total_tokens"] == 42
            assert [(item["token_id"], item["session_id"]) for item in codex["sessions"]] == [
                (1, "shared"),
                (2, "shared"),
            ]
            assert codex["sessions"][0]["active"] is True
            assert codex["sessions"][0]["turn_count"] == 2
            assert codex["sessions"][0]["models"] == ["gpt-a", "gpt-b"]
            assert datetime.fromisoformat(
                codex["sessions"][0]["first_seen_at"].replace("Z", "+00:00")
            ) == now.replace(tzinfo=timezone.utc)
            assert datetime.fromisoformat(
                codex["sessions"][0]["last_seen_at"].replace("Z", "+00:00")
            ) == (now + timedelta(seconds=2)).replace(tzinfo=timezone.utc)
            assert codex["sessions"][1]["active"] is False

            claude = payload["agents"][1]
            assert claude["session_count"] == 2
            assert claude["active_session_count"] == 1
            assert claude["turn_count"] == 3
            assert claude["total_tokens"] == 81
            assert (
                claude["sessions"][0]["token_id"],
                claude["sessions"][0]["session_id"],
            ) == (1, "shared")
            assert claude["sessions"][0]["active"] is True
            assert claude["sessions"][0]["models"] == ["haiku"]

            opencode = payload["agents"][2]
            assert opencode["session_count"] == 1
            assert opencode["turn_count"] == 1
            assert opencode["total_tokens"] == 50

            pi = payload["agents"][3]
            assert pi["id"] == "pi"
            assert pi["name"] == "Pi"
            assert pi["session_count"] == 1
            assert pi["turn_count"] == 1
            assert pi["total_tokens"] == 12
            assert pi["sessions"][0]["session_id"] == "pi-session"

            grok = payload["agents"][4]
            assert grok["id"] == "grok_build"
            assert grok["name"] == "Grok Build"
            assert grok["session_count"] == 1
            assert grok["turn_count"] == 1
            assert grok["total_tokens"] == 14
            assert grok["sessions"][0]["session_id"] == "grok-conversation"
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())


def test_monitoring_groups_mindcode_header_sessions_by_tenant() -> None:
    async def scenario() -> None:
        engine, sessions, client = await _monitoring_client()
        generated_at = datetime.now(timezone.utc)
        _, start, end = _today_window(
            generated_at,
            application_settings.get().display_timezone,
        )
        now = start + (end - start) / 2
        context = resolve_client_session({
            "X-MindCode-Session-ID": "shared-mindcode-session",
            "User-Agent": "OpenAI/Python 2.0.0",
        })
        try:
            empty = await client.get("/api/admin/monitoring/sources")
            assert empty.status_code == 200
            card = empty.json()["agents"][-1]
            assert card["id"] == "mindcode"
            assert card["name"] == "MindCode"
            assert card["session_count"] == 0

            async with sessions() as db:
                for request_id, token_id, tokens in (
                    ("mindcode-a", 1, 10),
                    ("mindcode-b", 1, 20),
                    ("mindcode-c", 2, 40),
                ):
                    db.add_all([
                        _routing(request_id, token_id, context.source, now),
                        _usage(
                            request_id, token_id, context.session_id,
                            "mindcode-model", tokens, now,
                        ),
                    ])
                db.add(SessionLease(
                    token_id=1,
                    session_id=context.session_id,
                    logical_model="mindcode-model",
                    channel_id=1,
                    last_used_at=now,
                    expires_at=generated_at.replace(tzinfo=None) + timedelta(hours=1),
                ))
                await db.commit()

            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            agents = response.json()["agents"]
            card = agents[-1]
            assert card["id"] == "mindcode"
            assert card["session_count"] == 2
            assert card["active_session_count"] == 1
            assert card["turn_count"] == 3
            assert card["total_tokens"] == 70
            by_token = {item["token_id"]: item for item in card["sessions"]}
            assert by_token[1]["session_id"] == context.session_id
            assert by_token[1]["turn_count"] == 2
            assert by_token[1]["total_tokens"] == 30
            assert by_token[1]["active"] is True
            assert by_token[2]["session_id"] == context.session_id
            assert by_token[2]["turn_count"] == 1
            assert by_token[2]["total_tokens"] == 40
            assert by_token[2]["active"] is False
            assert all(agent["session_count"] == 0 for agent in agents[:-1])
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())


def test_monitoring_extracts_only_earliest_user_summary_and_degrades(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, sessions, client = await _monitoring_client()
        generated_at = datetime.now(timezone.utc)
        _, start, end = _today_window(
            generated_at,
            application_settings.get().display_timezone,
        )
        now = start + (end - start) / 2
        expected_summary = ("Earliest user question " + "x" * 200)[:180]
        archive = tmp_path / "conversation.jsonl"
        archive.write_text(
            "not-json\n"
            + json.dumps({
                "request_id": "later",
                "messages": [
                    {"role": "assistant", "content": "secret assistant"},
                    {"role": "user", "content": "Later question"},
                ],
            })
            + "\n"
            + json.dumps({
                "request_id": "earlier",
                "messages": [
                    {"role": "system", "content": "secret system"},
                    {"role": "assistant", "content": "secret assistant"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "secret"},
                            {"type": "input_text", "text": "  \n"},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "  Earliest\n user"},
                            {"type": "image_url", "url": "secret"},
                            {"type": "text", "text": "question " + "x" * 200},
                        ],
                    },
                ],
            })
            + "\n"
            + json.dumps({
                "request_id": "no-user-first",
                "messages": [
                    {"role": "system", "content": "secret empty system"},
                    {"role": "assistant", "content": "secret empty assistant"},
                ],
            })
            + "\n"
            + json.dumps({
                "request_id": "later-user",
                "messages": [
                    {"role": "user", "content": "must-not-fallback"},
                ],
            })
            + "\n",
            encoding="utf-8",
        )
        invalid_archive = tmp_path / "invalid.jsonl"
        invalid_archive.write_text("{bad-json\n", encoding="utf-8")
        try:
            async with sessions() as db:
                db.add_all([
                    _routing("earlier", 1, "codex-session-id", now),
                    _routing("later", 1, "codex-session-id", now + timedelta(seconds=1)),
                    _routing("no-user-first", 2, "codex-session-id", now),
                    _routing("later-user", 2, "codex-session-id", now + timedelta(seconds=1)),
                    _routing("bad-json", 3, "codex-session-id", now),
                    _routing("missing-file", 4, "codex-session-id", now),
                    _usage("earlier", 1, "with-summary", "gpt", 10, now),
                    _usage("later", 1, "with-summary", "gpt", 10, now + timedelta(seconds=1)),
                    _usage("no-user-first", 2, "no-user", "gpt", 10, now),
                    _usage("later-user", 2, "no-user", "gpt", 10, now + timedelta(seconds=1)),
                    _usage("bad-json", 3, "bad-json", "gpt", 10, now),
                    _usage("missing-file", 4, "missing-file", "gpt", 10, now),
                    ConversationRecord(
                        conversation_id="with-summary",
                        request_id="earlier",
                        token_id=1,
                        model="gpt",
                        protocol="openai_responses",
                        file_path=str(archive),
                        created_at=now,
                    ),
                    ConversationRecord(
                        conversation_id="with-summary",
                        request_id="later",
                        token_id=1,
                        model="gpt",
                        protocol="openai_responses",
                        file_path=str(archive),
                        created_at=now + timedelta(seconds=1),
                    ),
                    ConversationRecord(
                        conversation_id="no-user",
                        request_id="no-user-first",
                        token_id=2,
                        model="gpt",
                        protocol="openai_responses",
                        file_path=str(archive),
                        created_at=now,
                    ),
                    ConversationRecord(
                        conversation_id="bad-json",
                        request_id="bad-json",
                        token_id=3,
                        model="gpt",
                        protocol="openai_responses",
                        file_path=str(invalid_archive),
                        created_at=now,
                    ),
                    ConversationRecord(
                        conversation_id="missing-file",
                        request_id="missing-file",
                        token_id=4,
                        model="gpt",
                        protocol="openai_responses",
                        file_path=str(tmp_path / "missing.jsonl"),
                        created_at=now,
                    ),
                ])
                await db.commit()

            real_to_thread = asyncio.to_thread
            threaded_functions: list[str] = []

            async def tracked_to_thread(function, *args, **kwargs):
                threaded_functions.append(function.__name__)
                return await real_to_thread(function, *args, **kwargs)

            def reject_read_text(*args, **kwargs):
                raise AssertionError("monitoring archives must be streamed")

            monkeypatch.setattr(
                monitoring_api.asyncio,
                "to_thread",
                tracked_to_thread,
            )
            monkeypatch.setattr(Path, "read_text", reject_read_text)
            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            sessions_payload = response.json()["agents"][0]["sessions"]
            summaries = {
                item["session_id"]: item["summary"] for item in sessions_payload
            }
            assert summaries == {
                "with-summary": expected_summary,
                "no-user": None,
                "bad-json": None,
                "missing-file": None,
            }
            assert len(summaries["with-summary"]) == 180
            serialized = json.dumps(response.json())
            assert "secret" not in serialized
            assert "Later question" not in serialized
            assert "must-not-fallback" not in serialized
            assert "_read_summary_files" in threaded_functions
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())


def test_monitoring_returns_all_known_agents_when_there_are_no_sessions() -> None:
    async def scenario() -> None:
        engine, _, client = await _monitoring_client()
        try:
            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            assert response.json()["agents"] == [
                {
                    "id": agent_id,
                    "name": name,
                    "request_count": 0,
                    "ungrouped_request_count": 0,
                    "session_count": 0,
                    "active_session_count": 0,
                    "total_tokens": 0,
                    "turn_count": 0,
                    "sessions": [],
                }
                for agent_id, name in (
                    ("codex", "Codex"),
                    ("claude_code", "Claude Code"),
                    ("opencode", "OpenCode"),
                    ("pi", "Pi"),
                    ("grok_build", "Grok Build"),
                    ("mindcode", "MindCode"),
                )
            ]
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())


def test_monitoring_router_is_registered_and_requires_admin() -> None:
    assert "/api/admin/monitoring/sources" in rotor_app.openapi()["paths"]

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=rotor_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/admin/monitoring/sources")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Session"

    asyncio.run(scenario())


def test_monitoring_counts_client_requests_without_inventing_sessions() -> None:
    async def scenario() -> None:
        engine, sessions, client = await _monitoring_client()
        _, start, end = _today_window(datetime.now(timezone.utc), application_settings.get().display_timezone)
        now = start + (end - start) / 2
        try:
            async with sessions() as db:
                for token_id, user_agent in enumerate((
                    "pi (darwin)", "codex-tui/1", "Codex Desktop/1",
                    "claude-cli/1", "opencode/1", "AsyncOpenAI/Python 3.8.0",
                    "MindCode/0.5.0",
                ), 1):
                    context = resolve_client_session({"User-Agent": user_agent})
                    # Same request ID across tenants, two routing attempts per request.
                    for _ in range(2):
                        row = _routing("shared-request", token_id, None, now)
                        row.feature_snapshot = context.routing_features()
                        db.add(row)
                    db.add(_usage("shared-request", token_id, "conv_generated", "model", 10, now))
                failed = _routing("failed-pi", 1, None, now)
                failed.feature_snapshot = resolve_client_session({"User-Agent": "pi/1"}).routing_features()
                db.add(failed)  # No ledger row: still a received request.
                context = resolve_client_session({
                    "User-Agent": "AsyncOpenAI/Python 3.8.0",
                    "x-mindcode-session-id": "mindcode",
                    "x-rotor-session-id": "explicit",
                })
                row = _routing("mindcode", 7, context.source, now)
                row.feature_snapshot = context.routing_features()
                db.add_all([row, _usage("mindcode", 7, context.session_id, "model", 20, now)])
                await db.commit()
            response = await client.get("/api/admin/monitoring/sources")
            assert response.status_code == 200
            agents = {a["id"]: a for a in response.json()["agents"]}
            for name, count, tokens in (("pi", 2, 10), ("codex", 2, 20), ("claude_code", 1, 10), ("opencode", 1, 10)):
                assert agents[name]["request_count"] == count
                assert agents[name]["ungrouped_request_count"] == count
                assert agents[name]["session_count"] == 0
                assert agents[name]["active_session_count"] == 0
                assert agents[name]["sessions"] == []
                assert agents[name]["total_tokens"] == tokens
            assert agents["mindcode"]["request_count"] == 2
            assert agents["mindcode"]["ungrouped_request_count"] == 1
            assert agents["mindcode"]["session_count"] == 1
            assert agents["mindcode"]["total_tokens"] == 30
            assert agents["mindcode"]["sessions"][0]["session_id"] == "explicit"
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())
