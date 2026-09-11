"""Data structures and deterministic aggregation for benchmark runs.

All durations are milliseconds.  No response text, request body, or
credential is retained in these structures.  The percentile implementation is
explicit (linear interpolation over the ``n - 1`` interval) so results do not
depend on a particular Python ``statistics`` implementation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from math import floor, isfinite
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence


def fingerprint_conversation_id(value: str | None) -> str | None:
    """Return a stable, non-reversible marker for a conversation identifier.

    Conversation IDs are sent as request headers so callers can correlate a
    run, but they may contain tenant/user information.  Persist only a short
    SHA-256 prefix in benchmark artifacts; ``None`` remains ``None``.
    """

    if value is None:
        return None
    text = str(value)
    if not text:
        return ""
    # Keep artifact rows idempotent when they are loaded and written again.
    # Accept our short prefix and a full SHA-256 marker produced by older
    # tooling; neither form reveals the original identifier.
    if re.fullmatch(r"sha256:[0-9a-fA-F]{16}|sha256:[0-9a-fA-F]{64}", text):
        return text.lower()
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _finite_intervals(value: Any) -> list[float]:
    """Normalize a possibly malformed interval collection to finite floats."""

    if not isinstance(value, (list, tuple)):
        return []
    intervals: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if isfinite(number):
            intervals.append(number)
    return intervals


_KNOWN_TERMINAL_EVENTS = {
    "[DONE]",
    "done",
    "response.completed",
    "response.failed",
    "response.cancelled",
    "response.error",
    "message_stop",
    "error",
    "stream_error",
}


def safe_terminal_event(value: str | None) -> str | None:
    """Keep terminal markers bounded when serializing untrusted SSE events."""

    if value is None:
        return None
    text = str(value).strip()
    return text if text in _KNOWN_TERMINAL_EVENTS else "terminal"


@dataclass
class RequestResult:
    """The safe, per-request record written to ``raw-results.jsonl``.

    ``started_at`` and ``finished_at`` are epoch seconds and are used only for
    diagnostics/RPS derivation.  They contain no request data.  ``chunk_intervals_ms``
    records inter-arrival gaps between parsed SSE data events; it is empty for
    non-streaming calls.
    """

    request_id: str
    conversation_id: str | None
    protocol: str
    stream: bool
    success: bool
    duration_ms: float
    ttft_ms: float | None = None
    chunk_count: int = 0
    chunk_intervals_ms: list[float] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    output_tokens_per_s: float | None = None
    status_code: int | None = None
    terminal_event: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    request_index: int | None = None

    @property
    def latency_ms(self) -> float:
        """Compatibility alias used by a few callers."""

        return self.duration_ms

    @property
    def chunks(self) -> int:
        """Compatibility alias for ``chunk_count``."""

        return self.chunk_count

    @property
    def error(self) -> str | None:
        """Compatibility alias for the safe error category/message."""

        return self.error_message

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe record, omitting no useful benchmark fields.

        The in-memory ``conversation_id`` is intentionally retained for the
        request/header API, while its serialized representation is a stable
        fingerprint so raw-results files never disclose the caller-supplied
        identifier.
        """

        data = asdict(self)
        data["conversation_id"] = fingerprint_conversation_id(self.conversation_id)
        data["terminal_event"] = safe_terminal_event(self.terminal_event)
        clean_intervals = _finite_intervals(self.chunk_intervals_ms)
        data["chunk_intervals_ms"] = clean_intervals
        # Keep explicit aliases for consumers that use the terminology from
        # the benchmark plan while retaining the unambiguous ``*_ms`` fields.
        data["latency_ms"] = self.duration_ms
        data["chunks"] = self.chunk_count
        data["chunk_intervals"] = list(clean_intervals)
        data["output_tokens_per_second"] = self.output_tokens_per_s
        return data

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequestResult":
        """Build a result from a JSON mapping (handy for report tooling)."""

        fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = {key: value[key] for key in fields if key in value}
        # Older hand-written result files may use ``latency_ms``/``chunks``.
        if "duration_ms" not in data and "latency_ms" in value:
            data["duration_ms"] = value["latency_ms"]
        if "chunk_count" not in data and "chunks" in value:
            data["chunk_count"] = value["chunks"]
        for boolean in ("success", "stream"):
            if boolean in data and isinstance(data[boolean], str):
                data[boolean] = data[boolean].strip().lower() in {"1", "true", "yes", "on"}
        for numeric in (
            "duration_ms", "ttft_ms", "input_tokens", "output_tokens", "total_tokens",
            "output_tokens_per_s", "status_code", "chunk_count", "request_index",
        ):
            if numeric in data and data[numeric] is not None:
                try:
                    data[numeric] = int(data[numeric]) if numeric in {"input_tokens", "output_tokens", "total_tokens", "status_code", "chunk_count", "request_index"} else float(data[numeric])
                except (TypeError, ValueError):
                    data.pop(numeric, None)
        if "chunk_intervals_ms" not in data and "chunk_intervals" in value:
            data["chunk_intervals_ms"] = value["chunk_intervals"]
        data["chunk_intervals_ms"] = _finite_intervals(data.get("chunk_intervals_ms"))
        data.setdefault("request_id", "unknown")
        data.setdefault("conversation_id", None)
        data.setdefault("protocol", "unknown")
        data.setdefault("stream", False)
        data.setdefault("success", False)
        data.setdefault("duration_ms", 0.0)
        return cls(**data)


def _values(values: Iterable[float | int | None]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        # NaN/inf make quantiles and reports non-reproducible.  Ignore them.
        if number == number and abs(number) != float("inf"):
            result.append(number)
    return result


def percentile(values: Iterable[float | int | None], p: float) -> float | None:
    """Return a deterministic linearly interpolated percentile.

    The rank is ``(n - 1) * p / 100``.  Empty or all-invalid samples return
    ``None`` rather than raising, which keeps sparse TTFT/usage reports useful.
    """

    if not 0 <= float(p) <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(_values(values))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (float(p) / 100.0)
    lower = floor(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def quantile_stats(values: Iterable[float | int | None]) -> dict[str, float | None]:
    """Summarize a sample with the percentiles used by the benchmark plan."""

    sample = _values(values)
    return {
        "count": len(sample),
        "p50": percentile(sample, 50),
        "p90": percentile(sample, 90),
        "p95": percentile(sample, 95),
        "p99": percentile(sample, 99),
        "max": max(sample) if sample else None,
    }


def _coerce_result(value: RequestResult | Mapping[str, Any]) -> RequestResult:
    if isinstance(value, RequestResult):
        return value
    return RequestResult.from_mapping(value)


def summarize_results(
    results: Iterable[RequestResult | Mapping[str, Any]],
    *,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Aggregate per-request results into a stable JSON-compatible mapping.

    ``elapsed_seconds`` should be the wall-clock duration of the measured
    window (warmup excluded).  If omitted, it is inferred from epoch fields;
    otherwise RPS is ``None`` instead of using a misleading sum of latencies.
    """

    rows = [_coerce_result(value) for value in results]
    total = len(rows)
    successes = sum(1 for row in rows if row.success)
    failures = total - successes

    if elapsed_seconds is None:
        starts = [row.started_at for row in rows if row.started_at is not None]
        ends = [row.finished_at for row in rows if row.finished_at is not None]
        if starts and ends:
            elapsed_seconds = max(0.0, max(ends) - min(starts))
    if elapsed_seconds is not None and elapsed_seconds > 0:
        rps = total / elapsed_seconds
        successful_rps = successes / elapsed_seconds
    else:
        rps = None
        successful_rps = None

    latencies = [row.duration_ms for row in rows]
    ttfts = [row.ttft_ms for row in rows]
    intervals = [gap for row in rows for gap in _finite_intervals(row.chunk_intervals_ms)]
    chunk_counts = [row.chunk_count for row in rows if row.stream]
    token_rates = [row.output_tokens_per_s for row in rows]
    output_tokens = [row.output_tokens for row in rows]
    input_tokens = [row.input_tokens for row in rows]
    total_tokens = [row.total_tokens for row in rows]

    error_counts = Counter(
        row.error_category or "unknown"
        for row in rows
        if not row.success
    )
    status_counts = Counter(
        str(row.status_code) if row.status_code is not None else "none"
        for row in rows
    )

    latency_stats = quantile_stats(latencies)
    ttft_stats = quantile_stats(ttfts)
    interval_stats = quantile_stats(intervals)
    chunk_stats = quantile_stats(chunk_counts)
    token_rate_stats = quantile_stats(token_rates)

    # Keep both nested and flat names: nested values are easier to consume in
    # code, while flat aliases make the Markdown/CLI output convenient.
    summary: dict[str, Any] = {
        "total_requests": total,
        "successful_requests": successes,
        "failed_requests": failures,
        "success_rate": (successes / total) if total else 0.0,
        "error_rate": (failures / total) if total else 0.0,
        "elapsed_seconds": elapsed_seconds,
        "rps": rps,
        "successful_rps": successful_rps,
        "latency_ms": latency_stats,
        "ttft_ms": ttft_stats,
        "chunk_count": chunk_stats,
        "chunk_interval_ms": interval_stats,
        "output_tokens_per_s": token_rate_stats,
        "errors": dict(sorted(error_counts.items())),
        "status_codes": dict(sorted(status_counts.items())),
        "usage": {
            "input_tokens": sum(value for value in input_tokens if value is not None),
            "output_tokens": sum(value for value in output_tokens if value is not None),
            "total_tokens": sum(value for value in total_tokens if value is not None),
            "requests_with_usage": sum(value is not None for value in total_tokens),
        },
        # Explicit aliases requested by the benchmark specification.
        "latency_p50_ms": latency_stats["p50"],
        "latency_p90_ms": latency_stats["p90"],
        "latency_p95_ms": latency_stats["p95"],
        "latency_p99_ms": latency_stats["p99"],
        "latency_max_ms": latency_stats["max"],
        "ttft_p50_ms": ttft_stats["p50"],
        "ttft_p90_ms": ttft_stats["p90"],
        "ttft_p95_ms": ttft_stats["p95"],
        "ttft_p99_ms": ttft_stats["p99"],
        "ttft_max_ms": ttft_stats["max"],
    }
    return summary


# Friendly aliases for callers that use singular terminology.
aggregate_results = summarize_results
compute_percentile = percentile


__all__ = [
    "RequestResult",
    "aggregate_results",
    "compute_percentile",
    "fingerprint_conversation_id",
    "percentile",
    "quantile_stats",
    "safe_terminal_event",
    "summarize_results",
]
