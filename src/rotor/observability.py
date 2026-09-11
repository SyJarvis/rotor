"""Bounded process-local performance samples and incremental accounting checks."""

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import math
import os
import threading
import time


logger = logging.getLogger(__name__)
_SUPPRESSED = ContextVar("rotor_metrics_suppressed", default=False)
_PROTOCOLS = ("chat", "responses", "anthropic", "images", "other")
_METRICS = (
    "db.acquire_ms", "db.sql_ms", "db.commit_ms", "db.hold_ms",
    "db.acquire_errors", "db.sql_errors", "db.commit_errors",
    "store.batch_size", "store.batch_ms", "store.queue_wait_ms", "store.drain_ms",
)


def metrics_suppressed() -> bool:
    return _SUPPRESSED.get()


@contextmanager
def suppress_metrics():
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def _now():
    return datetime.now(timezone.utc).isoformat()


class _Samples:
    def __init__(self, limit):
        self.values = deque(maxlen=limit)
        self.count = 0
        self.total = 0.0

    def add(self, value, now):
        self.count += 1
        self.total += value
        self.values.append((now, value))

    def snapshot(self, cutoff, unit):
        while self.values and self.values[0][0] < cutoff:
            self.values.popleft()
        values = sorted(value for _, value in self.values)

        def percentile(fraction):
            return values[math.ceil(len(values) * fraction) - 1] if values else None

        return {
            "count": self.count, "sum": self.total,
            "sample_count": len(values), "p50": percentile(0.5),
            "p95": percentile(0.95), "p99": percentile(0.99),
            "max": values[-1] if values else None, "unit": unit,
        }


class PerformanceMetrics:
    def __init__(self, *, window_seconds=300, sample_limit=2048):
        self.window_seconds = window_seconds
        self.sample_limit = sample_limit
        self.started_at = _now()
        self._lock = threading.RLock()
        self._metrics = {name: _Samples(sample_limit) for name in _METRICS}
        self._requests = {
            protocol: {
                "total": 0, "success": 0, "failed": 0, "cancelled": 0, "streams": 0,
                "latency_ms": _Samples(sample_limit), "ttft_ms": _Samples(sample_limit),
            }
            for protocol in _PROTOCOLS
        }

    def observe(self, name, value, protocol=None):
        if metrics_suppressed() or name not in self._metrics:
            return
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return
        with self._lock:
            self._metrics[name].add(value, time.monotonic())

    def request(self, protocol, success, cancelled, stream, latency_ms, ttft_ms):
        if metrics_suppressed():
            return
        protocol = protocol if protocol in _PROTOCOLS else "other"
        with self._lock:
            row = self._requests[protocol]
            row["total"] += 1
            row["cancelled" if cancelled else "success" if success else "failed"] += 1
            row["streams"] += int(bool(stream))
            for name, value in (("latency_ms", latency_ms), ("ttft_ms", ttft_ms)):
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    row[name].add(value, time.monotonic())

    def snapshot(self):
        with self._lock:
            cutoff = time.monotonic() - self.window_seconds
            return {
                "pid": os.getpid(), "started_at": self.started_at, "generated_at": _now(),
                "window_seconds": self.window_seconds, "sample_limit": self.sample_limit,
                "metrics": {
                    name: sample.snapshot(cutoff, "ms" if name.endswith("_ms") else "count")
                    for name, sample in self._metrics.items()
                },
                "requests": {
                    protocol: {
                        key: value.snapshot(cutoff, "ms") if isinstance(value, _Samples) else value
                        for key, value in row.items()
                    }
                    for protocol, row in self._requests.items()
                },
            }


performance_metrics = PerformanceMetrics()


class ReconciliationMonitor:
    """Compare changes since an in-memory baseline; never scan old usage rows."""

    def __init__(self, session_factory=None, *, cache_seconds=30):
        self._session_factory = session_factory
        self.cache_seconds = cache_seconds
        self._lock = asyncio.Lock()
        self._baseline = None
        self._previous = None
        self._ledger = {}
        self._cursor = 0
        self._result = None
        self._checked_monotonic = 0.0

    async def reset(self):
        async with self._lock:
            self._baseline = self._previous = self._result = None
            self._ledger = {}
            self._cursor = 0
            return self._response("baseline", None, "基线已重置；下次检查建立新基线。")

    def _response(self, status, checked_at, reason, *, requests=None, totals=None):
        return {
            "pid": os.getpid(),
            "started_at": performance_metrics.started_at,
            "status": status,
            "since": self._result["since"] if self._result else checked_at,
            "checked_at": checked_at, "reason": reason, "cached": False,
            "requests": requests or {"ledger": 0, "token": 0},
            "total_tokens": totals or {"ledger": 0, "token": 0, "quota": 0},
        }

    async def check(self):
        async with self._lock:
            if self._result and (
                self._result["status"] == "invalid"
                or time.monotonic() - self._checked_monotonic < self.cache_seconds
            ):
                return {**self._result, "cached": True}
            checked_at = _now()
            try:
                with suppress_metrics():
                    tokens, head, increments = await self._read_snapshot()
                result = self._compare(tokens, head, increments, checked_at)
            except NotImplementedError:
                result = self._response("error", checked_at, "增量对账目前仅支持 SQLite；其他数据库不保证账本 ID 按提交顺序可见。")
            except Exception:
                logger.exception("Incremental accounting reconciliation failed")
                result = self._response("error", checked_at, "读取对账快照失败；稍后重试。")
            self._result = result
            self._checked_monotonic = time.monotonic()
            return dict(result)

    async def _read_snapshot(self):
        from sqlalchemy import func, select, text
        from rotor.database import async_session_maker
        from rotor.models.token import Token
        from rotor.models.usage import UsageLedger

        async with (self._session_factory or async_session_maker)() as db:
            dialect = db.bind.dialect.name
            if dialect == "sqlite":
                await db.execute(text("BEGIN"))
            else:
                raise NotImplementedError("Incremental ID reconciliation requires SQLite")
            tokens = {
                row.id: (row.request_count, row.token_count, row.used_quota, row.quota, row.created_at)
                for row in (await db.execute(select(
                    Token.id, Token.request_count, Token.token_count, Token.used_quota,
                    Token.quota, Token.created_at,
                ))).all()
            }
            head = int(await db.scalar(select(func.max(UsageLedger.id))) or 0)
            increments = []
            if self._baseline is not None:
                increments = (await db.execute(select(
                    UsageLedger.token_id, func.count(UsageLedger.id), func.sum(UsageLedger.total_tokens),
                ).where(
                    UsageLedger.id > self._cursor, UsageLedger.id <= head,
                    UsageLedger.status == "success",
                ).group_by(UsageLedger.token_id))).all()
            return tokens, head, increments

    def _compare(self, tokens, head, increments, checked_at):
        if self._baseline is None:
            self._baseline = self._previous = tokens
            self._cursor = head
            result = self._response("baseline", checked_at, "已建立基线；仅检查此后新增的成功账本。")
            result["since"] = checked_at
            return result
        if set(tokens) != set(self._baseline):
            return self._response("invalid", checked_at, "Token 新建或删除，需重置基线。")
        if head < self._cursor:
            return self._response("invalid", checked_at, "账本 ID 回退，需重置基线。")
        for token_id, values in tokens.items():
            if values[3:] != self._previous[token_id][3:]:
                return self._response("invalid", checked_at, "Token 额度或身份发生变更，需重置基线。")
            if any(value < old for value, old in zip(values[:3], self._previous[token_id][:3])):
                return self._response("invalid", checked_at, "Token 计数下降或被重置，需重置基线。")
        for token_id, count, total in increments:
            if token_id not in tokens:
                return self._response("invalid", checked_at, "新增成功账本没有对应 Token，需检查数据后重置基线。")
            old_count, old_total = self._ledger.get(token_id, (0, 0))
            self._ledger[token_id] = (old_count + count, old_total + int(total or 0))
        token_deltas = {
            token_id: tuple(value - baseline for value, baseline in zip(values[:3], self._baseline[token_id][:3]))
            for token_id, values in tokens.items()
        }
        matches = all(
            delta == (self._ledger.get(token_id, (0, 0))[0],) + (self._ledger.get(token_id, (0, 0))[1],) * 2
            for token_id, delta in token_deltas.items()
        )
        self._previous = tokens
        self._cursor = head
        return self._response(
            "ok" if matches else "mismatch", checked_at,
            "基线后的增量一致。" if matches else "增量不一致；可能存在手工计数修改或记账差异，需进一步核查。",
            requests={"ledger": sum(value[0] for value in self._ledger.values()),
                      "token": sum(value[0] for value in token_deltas.values())},
            totals={"ledger": sum(value[1] for value in self._ledger.values()),
                    "token": sum(value[1] for value in token_deltas.values()),
                    "quota": sum(value[2] for value in token_deltas.values())},
        )


reconciliation_monitor = ReconciliationMonitor()
