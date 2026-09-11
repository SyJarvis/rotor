"""Offline contract tests for the benchmark MVP."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from benchmarks.clients import (
    BenchmarkClient,
    SSEEvent,
    build_anthropic_payload,
    build_chat_payload,
    build_responses_payload,
    endpoint_for_protocol,
    is_terminal_event,
    parse_sse,
    terminal_status,
)
from benchmarks.metrics import RequestResult, percentile, summarize_results
from benchmarks.mock_provider import MockProviderConfig, create_app
from benchmarks.report import build_environment, redact_url, safe_output_dir, write_artifacts
from benchmarks.runner import run_protocol
from benchmarks.validate_usage import compare_snapshots, snapshot_database


def test_sse_parser_and_terminal_events() -> None:
    events = parse_sse(
        ": heartbeat\n"
        "event: content_block_delta\n"
        "data: {\"type\":\"content_block_delta\",\n"
        "data: \"delta\":{\"text\":\"hi\"}}\n\n"
        "data: [DONE]\n\n"
    )
    assert len(events) == 2
    assert events[0].event == "content_block_delta"
    assert terminal_status(events[0], "anthropic") is None
    assert terminal_status(events[1], "chat") == "success"
    assert is_terminal_event(SSEEvent('{"type":"response.completed"}'), "responses")
    assert terminal_status(SSEEvent('{"type":"response.failed"}'), "responses") == "failure"
    assert terminal_status(SSEEvent('{"type":"response.cancelled"}'), "responses") == "failure"
    assert is_terminal_event(SSEEvent('{"type":"response.error"}'))
    assert terminal_status(SSEEvent('{"error":{"message":"boom"}}'), "responses") == "failure"
    assert terminal_status(SSEEvent('{"type":"message_stop"}', "message_stop"), "anthropic") == "success"


def test_metrics_percentiles_and_empty_samples() -> None:
    assert percentile([], 50) is None
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == 3.8499999999999996
    summary = summarize_results([])
    assert summary["total_requests"] == 0
    assert summary["success_rate"] == 0.0
    assert summary["latency_p95_ms"] is None
    assert summary["rps"] is None


def test_payloads_and_endpoint_resolution() -> None:
    chat_stream = build_chat_payload("m", "p", stream=True)
    assert chat_stream["messages"][-1]["content"] == "p"
    assert chat_stream["stream_options"] == {"include_usage": True}
    assert build_responses_payload("m", "p")["input"] == "p"
    assert build_anthropic_payload("m", "p")["messages"][0]["content"] == "p"
    assert endpoint_for_protocol("http://localhost:8000", "chat").endswith("/v1/chat/completions")
    assert endpoint_for_protocol("http://localhost:8000", "responses").endswith("/v1/responses")
    assert endpoint_for_protocol("http://localhost:8000", "anthropic").endswith("/anthropic/v1/messages")
    assert endpoint_for_protocol("http://localhost:9001/v1", "chat").endswith("/v1/chat/completions")


def test_client_connection_limits_can_match_high_concurrency() -> None:
    client = BenchmarkClient("http://mock", "k", "m", "chat", max_connections=200)
    try:
        assert client.max_connections == 200
        assert client.connection_limits is not None
        assert client.connection_limits.max_connections == 200
        assert client.connection_limits.max_keepalive_connections == 200
    finally:
        asyncio.run(client.close())


def test_mock_provider_all_protocols_nonstream_and_stream() -> None:
    async def exercise() -> None:
        app = create_app(
            MockProviderConfig(
                first_chunk_delay=0.001,
                chunk_interval=0.001,
                chunk_count=3,
                response_text="hello mock",
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            for protocol in ("chat", "responses", "anthropic"):
                client = BenchmarkClient(
                    "http://mock",
                    "secret-value",
                    "mock-model",
                    protocol,
                    http_client=wire,
                    conversation_id="fixed-conversation",
                )
                plain = await client.request(stream=False)
                streamed = await client.request(stream=True)
                assert plain.success and streamed.success
                assert streamed.ttft_ms is not None
                assert streamed.output_tokens == 2
                assert streamed.total_tokens == 10
                assert plain.request_id != streamed.request_id
                assert plain.conversation_id == streamed.conversation_id == "fixed-conversation"

    asyncio.run(exercise())


def test_stream_stops_at_terminal_marker() -> None:
    """A provider that keeps sending after [DONE] cannot stall the client."""

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_stream() -> StreamingResponse:
        async def body():
            yield 'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            yield "data: [DONE]\n\n"
            # This event must not be counted/read after the terminator.
            yield 'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    async def exercise() -> RequestResult:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            client = BenchmarkClient("http://mock", "k", "m", "chat", http_client=wire)
            return await client.request(stream=True)

    result = asyncio.run(exercise())
    assert result.success
    assert result.chunk_count == 1
    assert result.terminal_event == "[DONE]"


def test_runner_artifacts_exclude_prompt_and_key(tmp_path: Path) -> None:
    async def exercise() -> Path:
        app = create_app(MockProviderConfig(response_text="private response"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            return await run_protocol(
                base_url="http://mock",
                api_key="super-secret-key",
                model="mock-model",
                protocol="chat",
                stream=False,
                concurrency=2,
                requests=3,
                warmup=1,
                timeout=3,
                output_dir=tmp_path / "round",
                prompt="prompt should not be persisted",
                http_client=wire,
            )

    output = asyncio.run(exercise())
    assert {path.name for path in output.iterdir()} == {
        "environment.json", "raw-results.jsonl", "summary.json", "report.md"
    }
    environment = json.loads((output / "environment.json").read_text())
    raw = (output / "raw-results.jsonl").read_text()
    assert "super-secret-key" not in json.dumps(environment)
    assert "prompt should not be persisted" not in json.dumps(environment)
    assert "private response" not in raw
    assert len(raw.splitlines()) == 3
    assert json.loads((output / "summary.json").read_text())["total_requests"] == 3
    assert redact_url("https://u:pw@example.test/v1?api_key=secret").endswith("/v1")


def test_sqlite_snapshot_is_read_only_and_compares_deltas(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE request_logs (
          id INTEGER PRIMARY KEY, token_id INTEGER, channel_id INTEGER,
          model TEXT, request_model TEXT, prompt_tokens INTEGER,
          completion_tokens INTEGER, total_tokens INTEGER, success INTEGER,
          created_at TEXT
        );
        CREATE TABLE usage_ledger (
          id INTEGER PRIMARY KEY, request_id TEXT, model TEXT,
          request_protocol TEXT, prompt_tokens INTEGER,
          completion_tokens INTEGER, total_tokens INTEGER, status TEXT,
          created_at TEXT
        );
        CREATE TABLE request_attempts (
          id INTEGER PRIMARY KEY, request_id TEXT, attempt_index INTEGER,
          channel_id INTEGER, requested_model TEXT, request_protocol TEXT,
          outcome TEXT, started_at TEXT
        );
        CREATE TABLE tokens (
          id INTEGER PRIMARY KEY, key TEXT, name TEXT, used_quota INTEGER,
          request_count INTEGER, token_count INTEGER
        );
        CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, type TEXT);
        INSERT INTO tokens(id,key,name,used_quota,request_count,token_count)
          VALUES (1,'never-write-this-key','bench',0,0,0);
        """
    )
    connection.commit()
    connection.close()
    before = snapshot_database(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO request_logs VALUES (1,1,1,'m','m',8,2,10,1,'2026-09-01 00:00:00')")
    connection.execute("INSERT INTO usage_ledger VALUES (1,'req-1','m','openai_chat',8,2,10,'success','2026-09-01 00:00:00')")
    connection.execute("INSERT INTO request_attempts VALUES (1,'req-1',0,1,'m','openai_chat','success','2026-09-01 00:00:00')")
    # The cumulative counter also includes unrelated concurrent traffic; a
    # request-id-scoped comparison must not attribute all 20 units to req-1.
    connection.execute("UPDATE tokens SET used_quota=20,request_count=2,token_count=20 WHERE id=1")
    connection.commit()
    connection.close()
    after = snapshot_database(path, request_ids=["req-1"])
    comparison = compare_snapshots(before, after, request_ids=["req-1"])
    assert comparison["ok"]
    assert "request_logs_vs_usage_ledger_token_equality" in comparison["checks_skipped"]
    assert "tokens_vs_usage_ledger_used_quota" in comparison["checks_skipped"]
    assert comparison["tables"]["usage_ledger"]["numeric_delta"]["total_tokens"] == 10
    rendered = json.dumps(after)
    assert "never-write-this-key" not in rendered
    # mode=ro means attempting a write through the snapshot API is impossible;
    # this assertion also verifies the source DB remains queryable.
    assert sqlite3.connect(path).execute("SELECT used_quota FROM tokens").fetchone()[0] == 20


def test_output_collision_gets_suffix(tmp_path: Path) -> None:
    target = tmp_path / "round"
    target.mkdir()
    (target / "unrelated.txt").write_text("keep")
    selected = safe_output_dir(target)
    assert selected != target
    assert selected.name == "round-1"
    assert (target / "unrelated.txt").read_text() == "keep"


def test_anthropic_split_usage_is_normalized() -> None:
    async def exercise() -> RequestResult:
        app = create_app(MockProviderConfig(response_text="one two", chunk_count=2))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as wire:
            client = BenchmarkClient("http://mock", "k", "m", "anthropic", http_client=wire)
            return await client.request(stream=True)

    result = asyncio.run(exercise())
    assert result.input_tokens == 8
    assert result.output_tokens == 2
    assert result.total_tokens == 10


def test_validator_keeps_cumulative_token_baseline_in_time_window(tmp_path: Path) -> None:
    path = tmp_path / "window.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE tokens (id INTEGER PRIMARY KEY, name TEXT, used_quota INTEGER,
          request_count INTEGER, token_count INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE request_logs (id INTEGER PRIMARY KEY, total_tokens INTEGER, created_at TEXT);
        CREATE TABLE usage_ledger (id INTEGER PRIMARY KEY, request_id TEXT, total_tokens INTEGER, created_at TEXT);
        INSERT INTO tokens VALUES (1,'t',1000,10,1000,'2026-08-01 00:00:00','2026-08-01 00:00:00');
        """
    )
    db.commit()
    db.close()
    before = snapshot_database(path, since="2026-09-01T00:00:00Z")
    db = sqlite3.connect(path)
    db.execute("UPDATE tokens SET used_quota=1100,request_count=11,token_count=1100,updated_at='2026-09-01 00:00:01'")
    db.execute("INSERT INTO request_logs VALUES (1,100,'2026-09-01 00:00:01')")
    db.execute("INSERT INTO usage_ledger VALUES (1,'req-window',100,'2026-09-01 00:00:01')")
    db.commit()
    db.close()
    after = snapshot_database(path, since="2026-09-01T00:00:00Z")
    comparison = compare_snapshots(before, after)
    assert comparison["ok"]
    assert comparison["tables"]["tokens"]["numeric_delta"]["used_quota"] == 100


def test_validator_keeps_attempt_overlapping_time_window(tmp_path: Path) -> None:
    """An attempt that starts before ``since`` but finishes inside is retained."""

    path = tmp_path / "attempt-window.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE request_attempts (
          id INTEGER PRIMARY KEY, request_id TEXT, started_at TEXT,
          finished_at TEXT, outcome TEXT
        );
        INSERT INTO request_attempts VALUES
          (1, 'req-long', '2026-09-01 00:00:00', '2026-09-01 00:00:10', 'success'),
          (2, 'req-at-until', '2026-09-01 00:00:06', '2026-09-01 00:00:06', 'success');
        """
    )
    db.commit()
    db.close()

    snapshot = snapshot_database(
        path,
        since="2026-09-01T00:00:05Z",
        until="2026-09-01T00:00:06Z",
    )
    attempts = snapshot["tables"]["request_attempts"]
    assert attempts["row_count"] == 1
    assert attempts["request_ids"] == ["req-long"]


def test_environment_masks_camel_case_secrets() -> None:
    environment = build_environment(
        base_url="https://example.test/v1?api_key=secret",
        api_key="secret",
        parameters={"accessToken": "abc", "clientSecret": "def", "database_url": "raw-secret"},
    )
    rendered = json.dumps(environment)
    assert "abc" not in rendered and "def" not in rendered and "raw-secret" not in rendered


def test_validator_missing_accounting_evidence_is_not_ok() -> None:
    result = compare_snapshots(
        {"captured_at": 1, "database": {"type": "sqlite", "path": "/tmp/a"}, "tables": {}},
        {"captured_at": 2, "database": {"type": "sqlite", "path": "/tmp/a"}, "tables": {}},
    )
    assert result["ok"] is False
    assert result["checks_complete"] is False
    assert "after.usage_ledger" in result["evidence_missing"]

    unusable = {
        name: {
            "exists": True,
            "usable": False,
            "selected_columns": [],
            "row_count": 0,
            "numeric_sums": {},
            "rows": [],
            "request_ids": [],
        }
        for name in ("request_logs", "usage_ledger", "tokens")
    }
    result = compare_snapshots(
        {"database": {"path": "/tmp/a"}, "tables": unusable},
        {"database": {"path": "/tmp/a"}, "tables": unusable},
    )
    assert result["checks_complete"] is False
    assert "after.tokens" in result["evidence_missing"]


def test_validator_detects_duplicate_ledger_request_ids() -> None:
    def table(rows, columns, sums=None):
        return {
            "exists": True,
            "usable": True,
            "selected_columns": columns,
            "row_count": len(rows),
            "numeric_sums": sums or {},
            "rows": rows,
            "request_ids": sorted({str(row["request_id"]) for row in rows if row.get("request_id")}),
        }

    empty_logs = table([], ["id", "total_tokens"])
    empty_tokens = table([], ["id", "used_quota"])
    empty_attempts = table([], ["id", "request_id"])
    empty_channels = table([], ["id"])
    duplicate_rows = [
        {"id": 1, "request_id": "dup", "total_tokens": 5},
        {"id": 2, "request_id": "dup", "total_tokens": 5},
    ]
    before = {
        "database": {"type": "sqlite", "path": "/tmp/same.sqlite"},
        "tables": {
            "request_logs": empty_logs,
            "usage_ledger": table([], ["id", "request_id", "total_tokens"]),
            "request_attempts": empty_attempts,
            "tokens": empty_tokens,
            "channels": empty_channels,
        },
    }
    after = {
        "database": {"type": "sqlite", "path": "/tmp/same.sqlite"},
        "tables": {
            "request_logs": empty_logs,
            "usage_ledger": table(duplicate_rows, ["id", "request_id", "total_tokens"], {"total_tokens": 10}),
            "request_attempts": table(
                [{"id": 1, "request_id": "dup"}, {"id": 2, "request_id": "dup"}],
                ["id", "request_id"],
            ),
            "tokens": empty_tokens,
            "channels": empty_channels,
        },
    }
    result = compare_snapshots(before, after, request_ids=["dup"])
    assert result["ok"] is False
    assert {item["kind"] for item in result["mismatches"]} >= {"duplicate_request_ids"}
    assert result["duplicate_request_ids"] == {"dup": 2}
