"""Command-line runner for reproducible Rotor gateway measurements.

Example (from the ``rotor`` directory)::

    python -m benchmarks.runner --base-url http://127.0.0.1:8000 \
      --api-key "$ROTOR_API_KEY" --model glm-5.2 --protocol chat \
      --stream --concurrency 4 --requests 20 --warmup 2

Concurrency is closed-loop: a worker starts its next request only after its
previous request has completed.  ``--rate`` is an optional launch-rate cap;
it does not replace the concurrency limit and is reported separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import inspect
import os
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .clients import (
    DEFAULT_PROMPT,
    SUPPORTED_PROTOCOLS,
    BenchmarkClient,
    normalize_protocol,
)
from .metrics import RequestResult, percentile, summarize_results
from .report import build_environment, render_markdown, safe_output_dir, write_artifacts


DEFAULT_DURATION_SAMPLE_LIMIT = 4096


def _validate_positive(name: str, value: int) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _finite_positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _round_metric_stats(values: Sequence[float | int | None]) -> dict[str, Any]:
    """Summarize one metric across independent rounds deterministically."""

    sample: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            sample.append(number)
    if not sample:
        return {
            "count": 0,
            "median": None,
            "mean": None,
            "stdev": None,
            "stddev": None,
            "min": None,
            "max": None,
            "range": None,
        }
    mean = sum(sample) / len(sample)
    stdev = statistics.pstdev(sample) if len(sample) > 1 else 0.0
    return {
        "count": len(sample),
        "median": percentile(sample, 50),
        "mean": mean,
        "stdev": stdev,
        "stddev": stdev,
        "min": min(sample),
        "max": max(sample),
        "range": max(sample) - min(sample),
    }


def aggregate_round_summaries(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Create a safe aggregate for repeated rounds.

    Only known numeric/counter fields are copied from each summary; arbitrary
    mapping keys (which could contain a prompt or provider payload) are never
    persisted.  Quantile values are calculated across per-round statistics,
    not reconstructed as request-level quantiles.
    """

    metric_paths: dict[str, tuple[str, ...]] = {
        "rps": ("rps",),
        "latency_p50_ms": ("latency_ms", "p50"),
        "latency_p95_ms": ("latency_ms", "p95"),
        "latency_p99_ms": ("latency_ms", "p99"),
        "ttft_p50_ms": ("ttft_ms", "p50"),
        "ttft_p95_ms": ("ttft_ms", "p95"),
        "ttft_p99_ms": ("ttft_ms", "p99"),
    }

    def lookup(summary: Mapping[str, Any], path: tuple[str, ...]) -> Any:
        current: Any = summary
        for key in path:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
        return current

    round_records: list[dict[str, Any]] = []
    error_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    total_requests = successful_requests = failed_requests = 0
    elapsed_total = 0.0
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests_with_usage": 0}
    for index, summary in enumerate(summaries, start=1):
        if not isinstance(summary, Mapping):
            summary = {}
        total = int(summary.get("total_requests", 0) or 0)
        successful = int(summary.get("successful_requests", 0) or 0)
        failed = int(summary.get("failed_requests", max(0, total - successful)) or 0)
        elapsed = float(summary.get("elapsed_seconds", 0.0) or 0.0)
        total_requests += total
        successful_requests += successful
        failed_requests += failed
        if math.isfinite(elapsed) and elapsed > 0:
            elapsed_total += elapsed
        usage = summary.get("usage") if isinstance(summary.get("usage"), Mapping) else {}
        for field in usage_totals:
            try:
                usage_totals[field] += int(usage.get(field, 0) or 0)
            except (TypeError, ValueError):
                pass
        errors = summary.get("errors") if isinstance(summary.get("errors"), Mapping) else {}
        for name, count in errors.items():
            try:
                error_counts[str(name)] += int(count)
            except (TypeError, ValueError):
                continue
        statuses = summary.get("status_codes") if isinstance(summary.get("status_codes"), Mapping) else {}
        for code, count in statuses.items():
            try:
                status_counts[str(code)] += int(count)
            except (TypeError, ValueError):
                continue
        record: dict[str, Any] = {
            "round": index,
            "total_requests": total,
            "successful_requests": successful,
            "failed_requests": failed,
            "success_rate": float(summary.get("success_rate", 0.0) or 0.0),
            "error_rate": float(summary.get("error_rate", 0.0) or 0.0),
        }
        for name, path in metric_paths.items():
            record[name] = lookup(summary, path)
        round_records.append(record)

    metrics = {
        name: _round_metric_stats([record.get(name) for record in round_records])
        for name in metric_paths
    }
    aggregate: dict[str, Any] = {
        "mode": "rounds",
        "round_count": len(round_records),
        "rounds": round_records,
        "round_summaries": round_records,
        "metrics": metrics,
        "total_requests": total_requests,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "success_rate": successful_requests / total_requests if total_requests else 0.0,
        "error_rate": failed_requests / total_requests if total_requests else 0.0,
        "elapsed_seconds": elapsed_total if elapsed_total > 0 else None,
        "rps": total_requests / elapsed_total if elapsed_total > 0 else None,
        "successful_rps": successful_requests / elapsed_total if elapsed_total > 0 else None,
        "errors": dict(sorted(error_counts.items())),
        "status_codes": dict(sorted(status_counts.items())),
        "usage": usage_totals,
    }
    # Flat aliases make simple consumers/report templates convenient while the
    # nested ``metrics`` mapping retains count and dispersion for each value.
    # Keep overall ``rps`` numeric because the standard report renderer uses
    # that field for the aggregate throughput; expose round-level RPS stats
    # under an unambiguous alias instead.
    aggregate["rps_stats"] = metrics["rps"]
    aggregate.update({name: stats for name, stats in metrics.items() if name != "rps"})
    aggregate["latency_ms"] = {
        "count": metrics["latency_p50_ms"]["count"],
        "p50": metrics["latency_p50_ms"]["median"],
        "p95": metrics["latency_p95_ms"]["median"],
        "p99": metrics["latency_p99_ms"]["median"],
        # No request-level rows are retained at aggregate scope, so a true
        # maximum cannot be reconstructed.  Keep the old key as null and
        # expose the precisely named diagnostic value separately.
        "max": None,
        "max_round_p99": metrics["latency_p99_ms"]["max"],
    }
    aggregate["ttft_ms"] = {
        "count": metrics["ttft_p50_ms"]["count"],
        "p50": metrics["ttft_p50_ms"]["median"],
        "p95": metrics["ttft_p95_ms"]["median"],
        "p99": metrics["ttft_p99_ms"]["median"],
        "max": None,
        "max_round_p99": metrics["ttft_p99_ms"]["max"],
    }
    return aggregate


class _DurationCollector:
    """Bounded-memory accumulator for a duration run.

    Raw rows are written immediately by the caller.  Only the first
    ``sample_limit`` rows are retained for quantiles, making memory usage
    independent of the run duration; the summary advertises whether quantiles
    are exact or sample-limited.
    """

    def __init__(self, sample_limit: int = DEFAULT_DURATION_SAMPLE_LIMIT) -> None:
        self.sample_limit = _validate_positive("sample_limit", int(sample_limit))
        self.samples: list[RequestResult] = []
        self.total_requests = 0
        self.successful_requests = 0
        self.errors: Counter[str] = Counter()
        self.status_codes: Counter[str] = Counter()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests_with_usage": 0}

    def add(self, result: RequestResult) -> None:
        self.total_requests += 1
        if result.success:
            self.successful_requests += 1
        else:
            self.errors[result.error_category or "unknown"] += 1
        self.status_codes[str(result.status_code) if result.status_code is not None else "none"] += 1
        if result.input_tokens is not None:
            self.usage["input_tokens"] += int(result.input_tokens)
        if result.output_tokens is not None:
            self.usage["output_tokens"] += int(result.output_tokens)
        if result.total_tokens is not None:
            self.usage["total_tokens"] += int(result.total_tokens)
            self.usage["requests_with_usage"] += 1
        if len(self.samples) < self.sample_limit:
            self.samples.append(result)

    def summary(self, *, elapsed_seconds: float, target_duration: float, cancelled: bool = False) -> dict[str, Any]:
        summary = summarize_results(self.samples, elapsed_seconds=elapsed_seconds)
        total = self.total_requests
        successful = self.successful_requests
        summary.update(
            {
                "mode": "duration",
                "duration_target_seconds": target_duration,
                "cancelled": bool(cancelled),
                "total_requests": total,
                "successful_requests": successful,
                "failed_requests": total - successful,
                "success_rate": successful / total if total else 0.0,
                "error_rate": (total - successful) / total if total else 0.0,
                "errors": dict(sorted(self.errors.items())),
                "status_codes": dict(sorted(self.status_codes.items())),
                "usage": dict(self.usage),
                "quantile_sample_limit": self.sample_limit,
                "quantile_sample_size": len(self.samples),
                "quantiles_exact": total <= self.sample_limit,
            }
        )
        if elapsed_seconds > 0:
            summary["rps"] = total / elapsed_seconds
            summary["successful_rps"] = successful / elapsed_seconds
        else:
            summary["rps"] = None
            summary["successful_rps"] = None
        return summary


async def run_requests(
    client: BenchmarkClient,
    *,
    requests: int,
    concurrency: int,
    stream: bool,
    rate: float | None = None,
    prompt: str | None = None,
) -> tuple[list[RequestResult], float]:
    """Run a measured closed-loop batch and return results plus wall seconds."""

    _validate_positive("requests", int(requests))
    _validate_positive("concurrency", int(concurrency))
    if rate is not None:
        rate = float(rate)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("rate must be a finite non-negative number when provided")
    if rate == 0:
        rate = None
    semaphore = asyncio.Semaphore(int(concurrency))
    schedule_origin = time.perf_counter()

    async def one(index: int) -> RequestResult:
        if rate is not None:
            target = schedule_origin + index / float(rate)
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        async with semaphore:
            return await client.request(
                stream=stream,
                prompt=prompt,
                request_index=index,
            )

    started = time.perf_counter()
    # A fixed list keeps request indices deterministic and is appropriate for
    # the finite batches in the benchmark plan.
    gathered = await asyncio.gather(*(one(index) for index in range(int(requests))))
    elapsed = max(0.0, time.perf_counter() - started)
    return list(gathered), elapsed


async def run_duration_requests(
    client: BenchmarkClient,
    *,
    duration: float,
    concurrency: int,
    stream: bool,
    rate: float | None = None,
    prompt: str | None = None,
    on_result: Callable[[RequestResult], Any] | None = None,
) -> tuple[_DurationCollector, float, bool]:
    """Run closed-loop workers until a finite wall-clock duration elapses.

    Results are delivered to ``on_result`` as each request completes and are
    retained only in a bounded quantile sample.  A rate cap schedules global
    launch slots while the semaphore-like worker count remains the
    concurrency limit.  Cancellation stops workers, returns partial metrics,
    and lets the caller persist a safe partial round.
    """

    target_duration = _finite_positive("duration", duration)
    _validate_positive("concurrency", int(concurrency))
    if rate is not None:
        rate = float(rate)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("rate must be a finite non-negative number when provided")
        if rate == 0:
            rate = None
    collector = _DurationCollector()
    started = time.perf_counter()
    deadline = started + target_duration
    schedule_origin = started
    launch_lock = asyncio.Lock()
    next_index = 0

    async def deliver(result: RequestResult) -> None:
        collector.add(result)
        if on_result is not None:
            value = on_result(result)
            if inspect.isawaitable(value):
                await value

    async def worker() -> None:
        nonlocal next_index
        while True:
            if time.perf_counter() >= deadline:
                return
            async with launch_lock:
                index = next_index
                next_index += 1
                target = schedule_origin + index / rate if rate is not None else time.perf_counter()
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            # Do not start a new request once the duration window closes, but
            # allow a request already in flight to finish and be recorded.
            if time.perf_counter() >= deadline:
                return
            result = await client.request(stream=stream, prompt=prompt, request_index=index)
            await deliver(result)

    tasks = [asyncio.create_task(worker()) for _ in range(int(concurrency))]
    cancelled = False
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        cancelled = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        # Do not leave sibling workers running if a callback or unexpected
        # client implementation raises; callers can still persist any rows
        # delivered before the failure.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    elapsed = max(0.0, time.perf_counter() - started)
    return collector, elapsed, cancelled


async def run_round(
    client: BenchmarkClient,
    *,
    requests: int,
    concurrency: int,
    warmup: int = 0,
    stream: bool = False,
    rate: float | None = None,
    prompt: str = DEFAULT_PROMPT,
    on_warmup_complete: Callable[[], Any] | None = None,
) -> tuple[list[RequestResult], dict[str, Any], float]:
    """Run warmup (discarded) followed by one measured round.

    ``on_warmup_complete`` is invoked after warmup has finished and immediately
    before the measured requests.  It is used by optional instrumentation (for
    example process resource sampling) so warmup activity cannot contaminate
    measured-window metrics.  The callback is deliberately optional to retain
    the historical API.
    """

    if warmup < 0:
        raise ValueError("warmup must not be negative")
    if warmup:
        await run_requests(
            client,
            requests=warmup,
            concurrency=concurrency,
            stream=stream,
            rate=None,
            prompt=prompt,
        )
    if on_warmup_complete is not None:
        callback_result = on_warmup_complete()
        if inspect.isawaitable(callback_result):
            await callback_result
    results, elapsed = await run_requests(
        client,
        requests=requests,
        concurrency=concurrency,
        stream=stream,
        rate=rate,
        prompt=prompt,
    )
    summary = summarize_results(results, elapsed_seconds=elapsed)
    return results, summary, elapsed


def _round_parameters(
    *,
    proto: str,
    model: str | None = None,
    stream: bool,
    concurrency: int,
    requests: int,
    warmup: int,
    timeout: float,
    rate: float | None,
    max_tokens: int,
    prompt: str,
    round_index: int | None = None,
    round_count: int = 1,
    duration: float | None = None,
    random_conversation_id: bool = False,
    pid: int | None = None,
    sample_interval: float | None = None,
) -> dict[str, Any]:
    """Build the redacted, reproducibility-only scenario parameter mapping."""

    params: dict[str, Any] = {
        "protocol": proto,
        "stream": bool(stream),
        "concurrency": int(concurrency),
        "max_connections": int(concurrency),
        # ``requests`` is a finite-round control.  In duration mode the CLI
        # still accepts a placeholder for backwards compatibility, but it is
        # intentionally recorded as null so comparison never treats that
        # placeholder as part of the scenario identity.
        "requests": None if duration is not None else int(requests),
        "warmup": int(warmup),
        "timeout": float(timeout),
        "rate": rate,
        "max_tokens": int(max_tokens),
        "rounds": int(round_count),
        "duration": duration,
        "concurrency_model": "closed-loop",
        "conversation_mode": "random" if random_conversation_id else "fixed",
        "prompt_chars": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if model is not None:
        # Keep only the logical model name in the reproducibility metadata;
        # build_environment applies the established parameter sanitization.
        text = "".join(
            ch if (ch.isprintable() or ch in "\t ") else " "
            for ch in str(model)
        )
        params["model"] = " ".join(text.split())[:200]
    if round_index is not None:
        params["round"] = int(round_index)
    if pid is not None:
        params["pid"] = int(pid)
        params["sample_interval"] = sample_interval
    return params


def _resource_sampler(pid: int | None, sample_interval: float | None) -> Any | None:
    """Create an optional process sampler without making it a hard dependency."""

    if pid is None:
        return None
    try:
        from .resource_sampler import ProcessSampler
    except Exception as exc:  # pragma: no cover - only when optional module is absent
        raise RuntimeError(f"process sampling is unavailable: {type(exc).__name__}") from exc
    interval = 1.0 if sample_interval is None else float(sample_interval)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("sample_interval must be a finite positive number when --pid is used")
    return ProcessSampler(pid, interval=interval)


def _attach_resource(summary: dict[str, Any], stats: Any | None) -> dict[str, Any]:
    if stats is None:
        return summary
    try:
        value = stats.to_dict()
    except Exception:
        value = {"status": "error", "reason": "resource sampler failed"}
    # Keep resource data as a bounded aggregate; no process command line or
    # environment is ever copied into benchmark artifacts.
    summary["resource"] = value
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _render_aggregate_report(
    environment: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    *,
    protocol: str,
    stream: bool,
    concurrency: int,
    rate: float | None,
) -> str:
    """Render a compact aggregate report without copying arbitrary input."""

    # Reuse the established report header/results formatting for totals, then
    # append the across-round median/dispersion table.
    base = render_markdown(
        environment,
        aggregate,
        protocol=protocol,
        stream=stream,
        concurrency=concurrency,
        requests=int(aggregate.get("total_requests", 0) or 0),
        rate=rate,
    )
    metrics = aggregate.get("metrics") if isinstance(aggregate.get("metrics"), Mapping) else {}
    lines = [
        base.rstrip(),
        "",
        "Across-round aggregate RPS/elapsed values combine the measured windows "
        "from each round (duration rounds therefore use the sum of their windows). "
        "Request-level P90/chunk/output-token quantiles are not reconstructed; "
        "the displayed max is intentionally `n/a` because only per-round P99 "
        "values are retained.",
        "",
        "## Across-round statistics",
        "",
        "metric | median | stdev | min | max",
        "--- | ---: | ---: | ---: | ---:",
    ]
    for name in (
        "rps",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "ttft_p50_ms",
        "ttft_p95_ms",
        "ttft_p99_ms",
    ):
        stats = metrics.get(name) if isinstance(metrics, Mapping) else None
        stats = stats if isinstance(stats, Mapping) else {}
        lines.append(
            f"{name} | {stats.get('median', 'n/a')} | {stats.get('stdev', 'n/a')} | "
            f"{stats.get('min', 'n/a')} | {stats.get('max', 'n/a')}"
        )
    return "\n".join(lines) + "\n"


async def _run_duration_round(
    client: BenchmarkClient,
    *,
    output_dir: str | Path,
    base_url: str,
    api_key: str,
    database_url: str | None,
    proto: str,
    stream: bool,
    concurrency: int,
    requests: int,
    warmup: int,
    timeout: float,
    rate: float | None,
    max_tokens: int,
    prompt: str,
    duration: float,
    round_index: int,
    round_count: int,
    random_conversation_id: bool = False,
    pid: int | None = None,
    sample_interval: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Execute one duration round while streaming raw rows to disk."""

    directory = safe_output_dir(output_dir)
    raw_path = directory / "raw-results.jsonl"
    raw_handle = raw_path.open("w", encoding="utf-8")

    def write_result(result: RequestResult) -> None:
        raw_handle.write(
            json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        )
        raw_handle.flush()

    sampler = None
    duration_collector = _DurationCollector()
    elapsed = 0.0
    cancelled = False
    try:
        # Warmup is deliberately not sent through the duration collector/raw
        # file.  Keep it inside the cleanup scope so a failed warmup cannot
        # leak the open artifact handle.
        if warmup:
            await run_requests(
                client,
                requests=warmup,
                concurrency=concurrency,
                stream=stream,
                rate=None,
                prompt=prompt,
            )
        sampler = _resource_sampler(pid, sample_interval)
        if sampler is not None:
            sampler.start()
        # ``run_duration_requests`` already updates a collector.  Pass a
        # writer callback only; use its returned collector for the summary.
        duration_collector, elapsed, cancelled = await run_duration_requests(
            client,
            duration=duration,
            concurrency=concurrency,
            stream=stream,
            rate=rate,
            prompt=prompt,
            on_result=write_result,
        )
    finally:
        resource_stats = sampler.stop() if sampler is not None else None
        raw_handle.close()
    summary = duration_collector.summary(
        elapsed_seconds=elapsed,
        target_duration=float(duration),
        cancelled=cancelled,
    )
    _attach_resource(summary, resource_stats)
    params = _round_parameters(
        proto=proto,
        model=client.model,
        stream=stream,
        concurrency=concurrency,
        requests=requests,
        warmup=warmup,
        timeout=timeout,
        rate=rate,
        max_tokens=max_tokens,
        prompt=prompt,
        round_index=round_index,
        round_count=round_count,
        duration=float(duration),
        random_conversation_id=random_conversation_id,
        pid=pid,
        sample_interval=sample_interval,
    )
    environment = build_environment(
        base_url=base_url,
        api_key=api_key,
        parameters=params,
        database_url=database_url or os.getenv("DATABASE_URL"),
    )
    if resource_stats is not None:
        try:
            environment["resource"] = resource_stats.to_dict()
        except Exception:
            environment["resource"] = {"status": "error", "reason": "resource sampler failed"}
    _write_json(directory / "environment.json", environment)
    _write_json(directory / "summary.json", summary)
    (directory / "report.md").write_text(
        render_markdown(
            environment,
            summary,
            protocol=proto,
            stream=stream,
            concurrency=concurrency,
            requests=summary.get("total_requests", 0),
            rate=rate,
        ),
        encoding="utf-8",
    )
    return directory, summary


async def _run_finite_round(
    client: BenchmarkClient,
    *,
    output_dir: str | Path,
    base_url: str,
    api_key: str,
    database_url: str | None,
    proto: str,
    stream: bool,
    concurrency: int,
    requests: int,
    warmup: int,
    timeout: float,
    rate: float | None,
    max_tokens: int,
    prompt: str,
    round_index: int,
    round_count: int,
    random_conversation_id: bool = False,
    pid: int | None = None,
    sample_interval: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Execute and persist one ordinary finite round."""

    sampler = _resource_sampler(pid, sample_interval)
    sampler_started = False

    def start_sampler_after_warmup() -> None:
        nonlocal sampler_started
        if sampler is not None:
            sampler.start()
            sampler_started = True
    resource_stats = None
    try:
        results, summary, _elapsed = await run_round(
            client,
            requests=requests,
            concurrency=concurrency,
            warmup=warmup,
            stream=stream,
            rate=rate,
            prompt=prompt,
            on_warmup_complete=start_sampler_after_warmup,
        )
    finally:
        if sampler is not None and sampler_started:
            resource_stats = sampler.stop()
    _attach_resource(summary, resource_stats)
    params = _round_parameters(
        proto=proto,
        model=client.model,
        stream=stream,
        concurrency=concurrency,
        requests=requests,
        warmup=warmup,
        timeout=timeout,
        rate=rate,
        max_tokens=max_tokens,
        prompt=prompt,
        round_index=round_index,
        round_count=round_count,
        random_conversation_id=random_conversation_id,
        pid=pid,
        sample_interval=sample_interval,
    )
    environment = build_environment(
        base_url=base_url,
        api_key=api_key,
        parameters=params,
        database_url=database_url or os.getenv("DATABASE_URL"),
    )
    if resource_stats is not None:
        try:
            environment["resource"] = resource_stats.to_dict()
        except Exception:
            environment["resource"] = {"status": "error", "reason": "resource sampler failed"}
    report = render_markdown(
        environment,
        summary,
        protocol=proto,
        stream=stream,
        concurrency=concurrency,
        requests=requests,
        rate=rate,
    )
    path = write_artifacts(
        output_dir,
        results=results,
        summary=summary,
        environment=environment,
        report_text=report,
    )
    return path, summary


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("benchmarks") / "results" / stamp


async def _run_protocol_repeated(
    *,
    base_url: str,
    api_key: str,
    model: str,
    proto: str,
    stream: bool,
    concurrency: int,
    requests: int,
    warmup: int,
    timeout: float,
    output_dir: str | Path,
    rounds: int,
    duration: float | None,
    rate: float | None,
    conversation_id: str | None,
    random_conversation_id: bool,
    max_tokens: int,
    prompt: str,
    database_url: str | None,
    http_client: Any | None,
    transport: Any | None,
    pid: int | None,
    sample_interval: float | None,
) -> list[Path]:
    """Run one protocol for one or more independent rounds."""

    _validate_positive("rounds", int(rounds))
    if duration is not None:
        duration = _finite_positive("duration", duration)
    root = safe_output_dir(output_dir) if int(rounds) > 1 else Path(output_dir)
    client = BenchmarkClient(
        base_url,
        api_key,
        model,
        proto,
        timeout=timeout,
        conversation_id=conversation_id,
        random_conversation_id=random_conversation_id,
        max_tokens=max_tokens,
        prompt=prompt,
        max_connections=int(concurrency),
        http_client=http_client,
        transport=transport,
    )
    paths: list[Path] = []
    summaries: list[dict[str, Any]] = []
    try:
        for index in range(1, int(rounds) + 1):
            target = root / f"round-{index:03d}" if int(rounds) > 1 else output_dir
            if duration is None:
                path, summary = await _run_finite_round(
                    client,
                    output_dir=target,
                    base_url=base_url,
                    api_key=api_key,
                    database_url=database_url,
                    proto=proto,
                    stream=stream,
                    concurrency=concurrency,
                    requests=requests,
                    warmup=warmup,
                    timeout=timeout,
                    rate=rate,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    round_index=index,
                    round_count=rounds,
                    random_conversation_id=random_conversation_id,
                    pid=pid,
                    sample_interval=sample_interval,
                )
            else:
                path, summary = await _run_duration_round(
                    client,
                    output_dir=target,
                    base_url=base_url,
                    api_key=api_key,
                    database_url=database_url,
                    proto=proto,
                    stream=stream,
                    concurrency=concurrency,
                    requests=requests,
                    warmup=warmup,
                    timeout=timeout,
                    rate=rate,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    duration=duration,
                    round_index=index,
                    round_count=rounds,
                    random_conversation_id=random_conversation_id,
                    pid=pid,
                    sample_interval=sample_interval,
                )
            paths.append(path)
            summaries.append(summary)
    finally:
        await client.close()

    if int(rounds) > 1:
        aggregate = aggregate_round_summaries(summaries)
        resources = [
            summary.get("resource")
            for summary in summaries
            if isinstance(summary.get("resource"), Mapping)
        ]
        if resources:
            aggregate["resource_by_round"] = resources
        aggregate_params = _round_parameters(
            proto=proto,
            model=model,
            stream=stream,
            concurrency=concurrency,
            requests=requests,
            warmup=warmup,
            timeout=timeout,
            rate=rate,
            max_tokens=max_tokens,
            prompt=prompt,
            round_count=rounds,
            duration=duration,
            random_conversation_id=random_conversation_id,
            pid=pid,
            sample_interval=sample_interval,
        )
        aggregate_params.pop("round", None)
        environment = build_environment(
            base_url=base_url,
            api_key=api_key,
            parameters=aggregate_params,
            database_url=database_url or os.getenv("DATABASE_URL"),
        )
        if resources:
            environment["resource_by_round"] = resources
        _write_json(root / "aggregate.json", aggregate)
        _write_json(root / "summary.json", aggregate)
        _write_json(root / "environment.json", environment)
        (root / "report.md").write_text(
            _render_aggregate_report(
                environment,
                aggregate,
                protocol=proto,
                stream=stream,
                concurrency=concurrency,
                rate=rate,
            ),
            encoding="utf-8",
        )
    return paths


async def run_protocol(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    stream: bool,
    concurrency: int,
    requests: int,
    warmup: int,
    timeout: float,
    output_dir: str | Path,
    rounds: int = 1,
    duration: float | None = None,
    rate: float | None = None,
    conversation_id: str | None = None,
    random_conversation_id: bool = False,
    max_tokens: int = 128,
    prompt: str = DEFAULT_PROMPT,
    database_url: str | None = None,
    http_client: Any | None = None,
    transport: Any | None = None,
    pid: int | None = None,
    sample_interval: float | None = None,
) -> Path | list[Path]:
    """Run one protocol; repeated rounds return a list of round directories.

    With the default ``rounds=1, duration=None`` the historical single-path
    API and artifact layout are preserved.  ``duration`` takes precedence over
    ``requests`` for each round and streams raw rows to disk with bounded
    quantile memory.
    """

    proto = normalize_protocol(protocol)
    _validate_positive("rounds", int(rounds))
    _validate_positive("concurrency", int(concurrency))
    if duration is None:
        _validate_positive("requests", int(requests))
    elif int(requests) < 0:
        raise ValueError("requests must be non-negative when duration is set")
    if warmup < 0:
        raise ValueError("warmup must not be negative")
    if timeout <= 0 or not math.isfinite(float(timeout)):
        raise ValueError("timeout must be a finite positive number")
    if rate is not None and (not math.isfinite(float(rate)) or float(rate) < 0):
        raise ValueError("rate must be a finite non-negative number")
    if sample_interval is not None and (
        not math.isfinite(float(sample_interval)) or float(sample_interval) < 0
    ):
        raise ValueError("sample_interval must be a finite non-negative number")
    if pid is not None and sample_interval is not None and float(sample_interval) <= 0:
        raise ValueError("sample_interval must be positive when pid is provided")
    if pid is not None and int(pid) <= 0:
        raise ValueError("pid must be positive")
    return_value = await _run_protocol_repeated(
        base_url=base_url,
        api_key=api_key,
        model=model,
        proto=proto,
        stream=stream,
        concurrency=concurrency,
        requests=requests,
        warmup=warmup,
        timeout=timeout,
        output_dir=output_dir,
        rounds=rounds,
        duration=duration,
        rate=rate,
        conversation_id=conversation_id,
        random_conversation_id=random_conversation_id,
        max_tokens=max_tokens,
        prompt=prompt,
        database_url=database_url,
        http_client=http_client,
        transport=transport,
        pid=pid,
        sample_interval=sample_interval,
    )
    return return_value[0] if int(rounds) == 1 else return_value


async def run_benchmark(**kwargs: Any) -> list[Path]:
    """Programmatic entry point; ``protocol='all'`` creates protocol subtrees."""

    protocol = str(kwargs.pop("protocol", "chat")).lower()
    raw_output_dir = kwargs.pop("output_dir", None)
    output_dir = Path(raw_output_dir) if raw_output_dir else _default_output_dir()
    protocols = list(SUPPORTED_PROTOCOLS) if protocol == "all" else [normalize_protocol(protocol)]
    paths: list[Path] = []
    for proto in protocols:
        target = output_dir / proto if len(protocols) > 1 else output_dir
        result = await run_protocol(protocol=proto, output_dir=target, **kwargs)
        if isinstance(result, list):
            paths.extend(result)
        else:
            paths.append(result)
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a reproducible Rotor/OpenAI/Anthropic HTTP benchmark (no SDK required)."
    )
    parser.add_argument("--base-url", default=os.getenv("ROTOR_BASE_URL") or os.getenv("UPSTREAM_BASE_URL") or "http://127.0.0.1:8000", help="Rotor or direct upstream base URL")
    parser.add_argument("--api-key", default=os.getenv("ROTOR_API_KEY") or os.getenv("UPSTREAM_API_KEY") or "", help="API key (never written to results)")
    parser.add_argument("--model", default=os.getenv("ROTOR_MODEL") or os.getenv("UPSTREAM_MODEL") or "benchmark-model", help="Logical/provider model name")
    parser.add_argument("--protocol", choices=[*SUPPORTED_PROTOCOLS, "all"], default="chat")
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true", help="Measure SSE streaming")
    stream_group.add_argument("--no-stream", dest="stream", action="store_false", help="Measure JSON responses")
    parser.set_defaults(stream=False)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--requests", type=int, default=1, help="Measured requests per round (ignored when --duration is set)")
    parser.add_argument("--rounds", type=int, default=1, help="Independent measured rounds; writes round-XXX subdirectories and an aggregate")
    parser.add_argument("--duration", type=float, default=None, help="Run closed-loop workers for this many seconds per round (takes precedence over --requests)")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup requests, excluded from results")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--rate", type=float, default=None, help="Optional request launch cap (requests/s)")
    parser.add_argument("--pid", type=int, default=None, help="Optional target process PID for CPU/RSS sampling")
    parser.add_argument("--sample-interval", type=float, default=1.0, help="Process sampling interval in seconds (used with --pid)")
    parser.add_argument("--conversation-id", default=None, help="Fixed X-Conversation-Id")
    parser.add_argument("--random-conversation-id", action="store_true", help="Generate a conversation ID per request")
    parser.add_argument(
        "--conversation-mode",
        choices=("fixed", "random"),
        default="fixed",
        help="Conversation header strategy (alias for --random-conversation-id)",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="Optional DB URL recorded only as a redacted environment field")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt (not stored in result files)")
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    for name in ("concurrency", "rounds", "max_tokens"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.duration is None and args.requests <= 0:
        parser.error("--requests must be positive")
    if args.duration is not None and args.requests < 0:
        parser.error("--requests must be non-negative when --duration is set")
    if args.warmup < 0:
        parser.error("--warmup must not be negative")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be a finite positive number")
    if args.rate is not None and (not math.isfinite(args.rate) or args.rate < 0):
        parser.error("--rate must be a finite non-negative number")
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be positive")
    if not math.isfinite(args.sample_interval) or args.sample_interval < 0:
        parser.error("--sample-interval must be a finite non-negative number")
    if args.pid is not None and args.sample_interval <= 0:
        parser.error("--sample-interval must be positive when --pid is used")
    if args.rate == 0:
        args.rate = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    output_dir = args.output_dir or _default_output_dir()
    try:
        paths = asyncio.run(
            run_benchmark(
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                protocol=args.protocol,
                stream=args.stream,
                concurrency=args.concurrency,
                requests=args.requests,
                rounds=args.rounds,
                duration=args.duration,
                warmup=args.warmup,
                timeout=args.timeout,
                output_dir=output_dir,
                rate=args.rate,
                pid=args.pid,
                sample_interval=args.sample_interval,
                conversation_id=args.conversation_id,
                random_conversation_id=(args.random_conversation_id or args.conversation_mode == "random"),
                database_url=args.database_url,
                max_tokens=args.max_tokens,
                prompt=args.prompt,
            )
        )
    except KeyboardInterrupt:
        return 130
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())


__all__ = [
    "aggregate_round_summaries",
    "build_parser",
    "main",
    "run_benchmark",
    "run_duration_requests",
    "run_protocol",
    "run_requests",
    "run_round",
]
