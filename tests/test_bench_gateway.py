import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from scripts.bench_gateway import (
    DatabaseMetrics,
    build_parser,
    integrity_errors,
    main_async,
)
from rotor.database import create_database_engine


def test_database_metrics_counts_updates_and_releases_connections(tmp_path):
    async def exercise():
        engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")
        metrics = DatabaseMetrics(engine)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE counter (value INTEGER)"))
                await conn.execute(text("INSERT INTO counter VALUES (0)"))
            metrics.start()
            async with engine.connect() as conn:
                await conn.execute(text("UPDATE counter SET value=value+1"))
                await conn.commit()
                await conn.execute(text("SELECT value FROM counter"))
                await conn.rollback()
            snapshot = metrics.stop()
            assert snapshot["sql_attempts"] == {"UPDATE": 1, "SELECT": 1}
            assert snapshot["transaction_attempts"] == {"commit": 1, "rollback": 1}
            assert snapshot["connections_still_checked_out"] == 0
            assert snapshot["connection_hold_ms"]["count"] == 1
            assert snapshot["connection_hold_ms"]["total"] > 0
        finally:
            await engine.dispose()
    asyncio.run(exercise())


@pytest.mark.parametrize("missing", ["token_requests", "token_tokens", "archive_records"])
def test_integrity_check_rejects_lost_updates_and_missing_archive(missing):
    delta = dict.fromkeys((
        "routing_decisions", "request_attempts", "request_logs", "usage_ledger",
        "conversation_records", "completed_conversations", "unique_ledger_requests",
        "token_requests", "archive_records", "archive_unique_requests",
    ), 2)
    delta.update(dict.fromkeys(("token_tokens", "token_quota", "ledger_tokens", "log_tokens"), 22))
    results = [SimpleNamespace(success=True, total_tokens=11) for _ in range(2)]
    assert integrity_errors(delta, results, {}) == []
    delta[missing] -= 1
    assert any(error.startswith(missing + ":") for error in integrity_errors(delta, results, {}))


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_in_process_benchmark_reconciles_all_protocols_and_restores_patches(tmp_path, protocol, stream):
    from rotor.config import settings
    import rotor.api.v1.chat as chat
    import rotor.api.v1.responses as responses
    import rotor.database as database

    original = (settings.CONVERSATION_STORE_DIR, settings.CONVERSATION_STORE_ENABLED,
                chat.conversation_store, responses.conversation_store,
                database.engine, database.async_session_maker, chat.AsyncClient)
    outdir = tmp_path / "run"
    args = build_parser().parse_args([
        "--protocol", protocol, "--stream" if stream else "--no-stream",
        "--requests", "4", "--concurrency", "2", "--warmup", "2",
        "--output-dir", str(outdir),
    ])
    assert asyncio.run(main_async(args)) == 0
    assert original == (settings.CONVERSATION_STORE_DIR, settings.CONVERSATION_STORE_ENABLED,
                        chat.conversation_store, responses.conversation_store,
                        database.engine, database.async_session_maker, chat.AsyncClient)
    metrics = json.loads((outdir / "db-metrics.json").read_text())
    assert metrics["pragmas"] == {"journal_mode": "wal", "synchronous": 1, "busy_timeout": 5000}
    assert metrics["integrity_errors"] == []
    assert metrics["row_and_counter_delta"]["archive_records"] == 4
    assert metrics["connections_still_checked_out"] == 0
    if stream:
        results = [json.loads(line) for line in (outdir / "raw-results.jsonl").read_text().splitlines()]
        assert all(result["ttft_ms"] is None for result in results)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_http_benchmark_observes_chunks_before_stream_completion(tmp_path, protocol):
    outdir = tmp_path / "run"
    args = build_parser().parse_args([
        "--protocol", protocol,
        "--transport", "http", "--stream", "--requests", "8", "--concurrency", "4",
        "--warmup", "1", "--chunk-count", "3", "--chunk-interval", "0.05",
        "--output-dir", str(outdir),
    ])
    assert asyncio.run(main_async(args)) == 0
    results = [json.loads(line) for line in (outdir / "raw-results.jsonl").read_text().splitlines()]
    assert all(result["ttft_ms"] is not None for result in results)
    assert all(result["duration_ms"] - result["ttft_ms"] >= 50 for result in results)
    metrics = json.loads((outdir / "db-metrics.json").read_text())
    assert metrics["integrity_errors"] == []
    assert metrics["row_and_counter_delta"]["ledger_tokens"] == metrics["row_and_counter_delta"]["token_tokens"]


def test_reused_output_directory_keeps_metrics_with_each_round(tmp_path):
    outdir = tmp_path / "run"
    args = build_parser().parse_args([
        "--requests", "1", "--concurrency", "1", "--warmup", "0",
        "--output-dir", str(outdir),
    ])
    assert asyncio.run(main_async(args)) == 0
    first = (outdir / "db-metrics.json").read_text()
    args.requests = 2
    assert asyncio.run(main_async(args)) == 0
    assert (outdir / "db-metrics.json").read_text() == first
    second = json.loads((tmp_path / "run-1" / "db-metrics.json").read_text())
    assert second["row_and_counter_delta"]["usage_ledger"] == 2
