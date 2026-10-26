"""Bounded process-local performance samples."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import math
import os
import threading
import time


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
