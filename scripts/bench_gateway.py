"""Isolated Rotor benchmark with mock upstream, SQLite, and DB instrumentation.

Use --transport http for streaming timing: in-process ASGI transports buffer
response bodies and cannot measure TTFT. HTTP mode runs both apps on loopback.
All database/archive data is temporary; artifacts include db-metrics.json.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
import json
import socket
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import event


class DatabaseMetrics:
    """Engine event counts and checkout-to-checkin duration, not pool wait time.

    SQL and transaction events count attempted operations. Integrity checks
    independently verify persisted effects; errors are reported separately.
    """

    def __init__(self, engine):
        self.enabled = False
        self.sql = Counter()
        self.transactions = Counter()
        self.holds_ms = []
        self.checked_out = {}
        event.listen(engine.sync_engine, "before_cursor_execute", self._sql)
        event.listen(engine.sync_engine, "commit", lambda conn: self._transaction("commit"))
        event.listen(engine.sync_engine, "rollback", lambda conn: self._transaction("rollback"))
        event.listen(engine.sync_engine, "handle_error", lambda ctx: self._transaction("error"))
        event.listen(engine.sync_engine, "checkout", self._checkout)
        event.listen(engine.sync_engine, "checkin", self._checkin)

    def _sql(self, conn, cursor, statement, parameters, context, executemany):
        if self.enabled:
            self.sql[statement.lstrip().split(None, 1)[0].upper()] += 1

    def _transaction(self, kind):
        if self.enabled:
            self.transactions[kind] += 1

    def _checkout(self, connection, record, proxy):
        self.checked_out[id(record)] = time.perf_counter()

    def _checkin(self, connection, record):
        started = self.checked_out.pop(id(record), None)
        if self.enabled and started is not None:
            self.holds_ms.append((time.perf_counter() - started) * 1000)

    def start(self):
        if self.checked_out:
            raise RuntimeError("DB connections still checked out at measurement boundary")
        self.sql.clear()
        self.transactions.clear()
        self.holds_ms.clear()
        self.enabled = True

    def stop(self):
        self.enabled = False
        ordered = sorted(self.holds_ms)
        return {
            "sql_attempts": dict(self.sql),
            "transaction_attempts": dict(self.transactions),
            "connection_hold_ms": {
                "count": len(ordered),
                "total": sum(ordered),
                "max": max(ordered, default=0),
                "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else None,
            },
            "connections_still_checked_out": len(self.checked_out),
            "note": "Engine events count attempts; connection hold time is not pool acquisition wait time.",
        }


@asynccontextmanager
async def loopback_server(app):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(
        app, lifespan="off", log_level="error", access_log=False,
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async def started():
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Loopback server exited before startup")
                await asyncio.sleep(.01)
        await asyncio.wait_for(started(), 10)
        yield url, server
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            sock.close()


async def settle_requests(server, timeout):
    # The HTTP client can consume the SSE terminator before accounting finishes.
    # Uvicorn retains run_asgi tasks until response cleanup completes.
    if server is not None:
        tasks = list(server.server_state.tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout)


async def drain_store(store, timeout):
    if hasattr(store, "drain"):
        await store.drain(timeout=timeout)
    else:
        # Supports comparison with the pre-batching worker.
        await store.shutdown(timeout=timeout)
        store.attach()


def database_snapshot(db_path, archive_dir):
    with sqlite3.connect(db_path) as db:
        tables = ("routing_decisions", "request_attempts", "request_logs",
                  "usage_ledger", "session_leases", "session_lease_events",
                  "conversation_records")
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in tables}
        counts.update(zip(("token_requests", "token_tokens", "token_quota"), db.execute(
            "SELECT coalesce(sum(request_count),0), coalesce(sum(token_count),0), "
            "coalesce(sum(used_quota),0) FROM tokens"
        ).fetchone()))
        counts["ledger_tokens"] = db.execute(
            "SELECT coalesce(sum(total_tokens),0) FROM usage_ledger"
        ).fetchone()[0]
        counts["log_tokens"] = db.execute(
            "SELECT coalesce(sum(total_tokens),0) FROM request_logs"
        ).fetchone()[0]
        counts["unique_ledger_requests"] = db.execute(
            "SELECT count(distinct request_id) FROM usage_ledger"
        ).fetchone()[0]
        counts["completed_conversations"] = db.execute(
            "SELECT count(*) FROM conversation_records WHERE status='success'"
        ).fetchone()[0]
        pragmas = {name: db.execute(f"PRAGMA {name}").fetchone()[0]
                   for name in ("journal_mode",)}
    lines = [json.loads(line) for path in archive_dir.rglob("*.jsonl")
             for line in path.read_text().splitlines() if line.strip()]
    counts["archive_records"] = len(lines)
    counts["archive_unique_requests"] = len({line["request_id"] for line in lines})
    return counts, pragmas


def integrity_errors(delta, results, drop_stats):
    errors = []
    successes = [result for result in results if result.success]
    expected_requests = len(successes)
    expected_tokens = sum(result.total_tokens or 0 for result in successes)
    if len(successes) != len(results):
        errors.append("Not all measured requests succeeded")
    if any(result.total_tokens is None for result in successes):
        errors.append("Successful response missing usage; token reconciliation unavailable")
    for name in ("routing_decisions", "request_attempts", "request_logs", "usage_ledger",
                 "conversation_records", "completed_conversations", "unique_ledger_requests",
                 "token_requests", "archive_records", "archive_unique_requests"):
        if delta[name] != expected_requests:
            errors.append(f"{name}: expected {expected_requests}, got {delta[name]}")
    for name in ("token_tokens", "token_quota", "ledger_tokens", "log_tokens"):
        if delta[name] != expected_tokens:
            errors.append(f"{name}: expected {expected_tokens}, got {delta[name]}")
    if any(drop_stats.values()):
        errors.append(f"Conversation events dropped: {drop_stats}")
    return errors


def build_parser():
    p = argparse.ArgumentParser(description="Isolated Rotor gateway benchmark")
    p.add_argument("--protocol", default="chat", choices=["chat", "responses", "anthropic"])
    stream = p.add_mutually_exclusive_group()
    stream.add_argument("--stream", dest="stream", action="store_true")
    stream.add_argument("--no-stream", dest="stream", action="store_false")
    p.set_defaults(stream=False)
    p.add_argument("--transport", choices=["in-process", "http"], default="in-process")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--requests", type=int, default=40)
    p.add_argument("--warmup", type=int, default=5)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-tokens", type=int, default=16)
    p.add_argument("--chunk-count", type=int, default=2)
    p.add_argument("--chunk-interval", type=float, default=0.0)
    p.add_argument("--output-dir", type=Path, default=None)
    return p


async def main_async(args):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from benchmarks.clients import BenchmarkClient
    from benchmarks.metrics import summarize_results
    from benchmarks.mock_provider import MockProviderConfig, create_app as create_mock_app
    from benchmarks.report import build_environment, render_markdown, write_artifacts
    from benchmarks.runner import run_requests
    import rotor.database as db_mod
    import rotor.conversations.store as store_mod
    import rotor.api.v1.chat as chat_mod
    import rotor.api.v1.anthropic as anthropic_mod
    import rotor.api.v1.responses as responses_mod
    import rotor.api.v1.images as images_mod
    from rotor.config import settings
    from rotor.main import app as gateway_app
    from rotor.models.channel import Channel
    from rotor.models.token import Token

    async with AsyncExitStack() as stack:
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rotor-bench-")))
        archive_dir = tmp / "conversations"
        db_path = tmp / "bench.db"
        engine = db_mod.create_database_engine(f"sqlite+aiosqlite:///{db_path}")
        stack.push_async_callback(engine.dispose)
        metrics = DatabaseMetrics(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)
            pragmas = {name: (await conn.exec_driver_sql(f"PRAGMA {name}")).scalar()
                       for name in ("journal_mode", "synchronous", "busy_timeout")}

        endpoints = (chat_mod, anthropic_mod, responses_mod, images_mod)
        stack.enter_context(patch.object(db_mod, "engine", engine))
        for module in (db_mod, store_mod, *endpoints):
            stack.enter_context(patch.object(module, "async_session_maker", sessions))
        stack.enter_context(patch.object(settings, "CONVERSATION_STORE_ENABLED", True))
        stack.enter_context(patch.object(settings, "CONVERSATION_STORE_DIR", str(archive_dir)))
        bench_store = store_mod.ConversationStore()
        for module in endpoints:
            stack.enter_context(patch.object(module, "conversation_store", bench_store, create=True))
        bench_store.attach()
        stack.push_async_callback(bench_store.shutdown, timeout=args.timeout)

        mock_app = create_mock_app(MockProviderConfig(
            chunk_count=args.chunk_count, chunk_interval=args.chunk_interval,
        ))
        gateway_server = None
        if args.transport == "http":
            original_client = httpx.AsyncClient
            def local_client(*positional, **kwargs):
                kwargs.setdefault("trust_env", False)
                return original_client(*positional, **kwargs)
            for module in endpoints:
                stack.enter_context(patch.object(module, "AsyncClient", local_client))
            upstream_url, _ = await stack.enter_async_context(loopback_server(mock_app))
            gateway_url, gateway_server = await stack.enter_async_context(loopback_server(gateway_app))
            wire = await stack.enter_async_context(httpx.AsyncClient(
                base_url=gateway_url, trust_env=False,
                limits=httpx.Limits(max_connections=args.concurrency),
            ))
        else:
            upstream_url, gateway_url = "http://mock-upstream", "http://gateway"
            mock_transport = httpx.ASGITransport(app=mock_app)
            original_client = httpx.AsyncClient
            def mock_client(*positional, **kwargs):
                kwargs["transport"] = mock_transport
                return original_client(*positional, **kwargs)
            for module in endpoints:
                stack.enter_context(patch.object(module, "AsyncClient", mock_client))
            wire = await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway_app), base_url=gateway_url,
            ))

        async with sessions() as db:
            db.add(Token(key="sk-bench-test-key-001234", name="bench", user_id="bench-user",
                         request_count=0, token_count=0, used_quota=0, quota=None,
                         enabled=True, expired=False))
            db.add(Channel(name="mock", type="openai", key="mock-key",
                           base_url=upstream_url + "/v1", models=["bench-model"],
                           model_mapping={}, priority=1, weight=1, enabled=True,
                           test_only=False, protocol="openai", extra={}))
            await db.commit()

        client = BenchmarkClient(gateway_url, "sk-bench-test-key-001234", "bench-model",
                                 args.protocol, timeout=args.timeout,
                                 max_tokens=args.max_tokens, http_client=wire)
        for i in range(args.warmup):
            result = await client.request(stream=args.stream, request_index=-1)
            if not result.success:
                raise RuntimeError(f"Warmup request failed: {result.error_category}")
        await settle_requests(gateway_server, args.timeout)
        await drain_store(bench_store, args.timeout)
        before, _ = database_snapshot(db_path, archive_dir)
        if any(bench_store.drop_stats().values()):
            raise RuntimeError("Conversation events dropped during warmup")
        metrics.start()
        results, elapsed = await run_requests(
            client, requests=args.requests, concurrency=args.concurrency,
            stream=args.stream, prompt="Say ok.",
        )
        drain_started = time.perf_counter()
        await settle_requests(gateway_server, args.timeout)
        await drain_store(bench_store, args.timeout)
        drain_seconds = time.perf_counter() - drain_started
        measured = metrics.stop()
        after, _ = database_snapshot(db_path, archive_dir)
        delta = {key: after[key] - before[key] for key in before}
        errors = integrity_errors(delta, results, bench_store.drop_stats())
        if measured["connections_still_checked_out"]:
            errors.append("DB connections still checked out after drain")
        measured.update(pragmas=pragmas, row_and_counter_delta=delta,
                        post_response_drain_seconds=drain_seconds,
                        integrity_errors=errors)
        if args.transport == "in-process":
            for result in results:
                result.ttft_ms = None
                result.chunk_intervals_ms = []
        summary = summarize_results(results, elapsed_seconds=elapsed)
        timing_note = ("HTTP loopback on both hops; TTFT includes local scheduling."
                       if args.transport == "http" else
                       "TTFT and chunk intervals unavailable: ASGITransport buffers bodies.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        outdir = args.output_dir or Path("benchmarks/results") / f"bench-{stamp}"
        env = build_environment(base_url=gateway_url, api_key="sk-bench-test-key-001234",
                                parameters=vars(args))
        report = render_markdown(env, summary, protocol=args.protocol, stream=args.stream,
                                 concurrency=args.concurrency, requests=args.requests, rate=None)
        report += f"\n{timing_note}\n\nDB integrity: {'FAILED' if errors else 'passed'}. See db-metrics.json.\n"
        path = write_artifacts(outdir, results=results, summary=summary,
                               environment=env, report_text=report)
        (path / "db-metrics.json").write_text(json.dumps(measured, indent=2) + "\n")
        print(f"results: {path}")
        print(f"success: {summary['successful_requests']}/{summary['total_requests']} "
              f"rps={summary.get('rps')} latency_ms={summary['latency_ms']}")
        print(timing_note)
        if args.stream and args.transport == "http":
            print(f"ttft_ms={summary.get('ttft_ms')}")
        print(json.dumps(measured, indent=2))
        return 1 if errors else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
