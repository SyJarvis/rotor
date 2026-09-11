"""Optional, dependency-light CPU/RSS sampling for a target process.

The benchmark runner deliberately does not require a process-inspection
dependency.  This module can be used on its own (or embedded by a caller)
when a PID is available.  :mod:`psutil` is used when installed; otherwise a
small ``ps`` based reader is used on macOS/Linux.  The reader only receives a
PID and never captures a process command line or environment, which keeps
credentials out of the resulting artifacts.

Typical use::

    sampler = ProcessSampler(os.getpid(), interval=0.25)
    sampler.start()
    run_the_benchmark()
    resources = sampler.stop()
    print(resources.to_dict())

``ResourceStats.status`` is ``"ok"`` when at least one sample was read and
``"unavailable"`` when the platform/PID cannot be inspected.  Sampling is
best-effort and must not make a benchmark fail merely because an optional
measurement is unavailable.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class ProcessSampleSource(Protocol):
    """Callable source used by :class:`ProcessSampler`.

    A source may return a :class:`ProcessSample`, a mapping with CPU/RSS
    fields, a ``(cpu_percent, rss_bytes)`` pair, or ``None`` when no sample is
    available.  The permissive shape makes deterministic unit-test fakes easy
    to write while keeping the public sampler API small.
    """

    def __call__(self, pid: int) -> Any:
        ...


@dataclass(frozen=True)
class ProcessSample:
    """One point-in-time process measurement.

    ``cpu_percent`` is process CPU utilisation as a percentage (not a
    fraction).  ``rss_bytes`` is resident memory in bytes.
    """

    cpu_percent: float | None = None
    rss_bytes: int | None = None
    timestamp: float | None = None


@dataclass
class ResourceStats:
    """Aggregated process resource measurements.

    The canonical field names are explicit about units.  ``to_dict`` also
    emits short aliases (``avg_cpu_percent``, ``peak_rss_bytes`` etc.) for
    consumers that used earlier benchmark notes.
    """

    pid: int | None
    status: str = "unavailable"
    reason: str | None = None
    sample_count: int = 0
    interval_seconds: float | None = None
    duration_seconds: float | None = None
    cpu_percent_avg: float | None = None
    cpu_percent_max: float | None = None
    rss_bytes_avg: float | None = None
    rss_bytes_peak: int | None = None
    time_unit: str = "seconds"
    cpu_unit: str = "percent"
    rss_unit: str = "bytes"
    _samples: list[ProcessSample] = field(default_factory=list, repr=False)

    @property
    def avg_cpu_percent(self) -> float | None:
        return self.cpu_percent_avg

    @property
    def max_cpu_percent(self) -> float | None:
        return self.cpu_percent_max

    @property
    def avg_rss_bytes(self) -> float | None:
        return self.rss_bytes_avg

    @property
    def peak_rss_bytes(self) -> int | None:
        return self.rss_bytes_peak

    @property
    def avg_cpu(self) -> float | None:
        return self.cpu_percent_avg

    @property
    def max_cpu(self) -> float | None:
        return self.cpu_percent_max

    @property
    def avg_rss(self) -> float | None:
        return self.rss_bytes_avg

    @property
    def peak_rss(self) -> int | None:
        return self.rss_bytes_peak

    @property
    def samples(self) -> tuple[ProcessSample, ...]:
        """Read-only view of samples, useful for diagnostics/tests."""

        return tuple(self._samples)

    def to_dict(self, *, include_samples: bool = False) -> dict[str, Any]:
        """Return a JSON-safe summary.

        Raw samples are omitted by default: benchmark reports only need the
        aggregate and omitting them reduces accidental retention of detailed
        timing information.  ``include_samples`` is available for local
        debugging and still contains no command line or environment data.
        """

        result: dict[str, Any] = {
            "pid": self.pid,
            "status": self.status,
            "reason": self.reason,
            "sample_count": int(self.sample_count),
            "interval_seconds": self.interval_seconds,
            "duration_seconds": self.duration_seconds,
            "time_unit": self.time_unit,
            "cpu_unit": self.cpu_unit,
            "rss_unit": self.rss_unit,
            "cpu_percent_avg": self.cpu_percent_avg,
            "cpu_percent_max": self.cpu_percent_max,
            "rss_bytes_avg": self.rss_bytes_avg,
            "rss_bytes_peak": self.rss_bytes_peak,
            # Compatibility aliases used by reports and simple scripts.
            "avg_cpu_percent": self.cpu_percent_avg,
            "max_cpu_percent": self.cpu_percent_max,
            "avg_rss_bytes": self.rss_bytes_avg,
            "peak_rss_bytes": self.rss_bytes_peak,
            "avg_cpu": self.cpu_percent_avg,
            "max_cpu": self.cpu_percent_max,
            "avg_rss": self.rss_bytes_avg,
            "peak_rss": self.rss_bytes_peak,
        }
        if include_samples:
            result["samples"] = [
                {
                    "timestamp": sample.timestamp,
                    "cpu_percent": sample.cpu_percent,
                    "rss_bytes": sample.rss_bytes,
                }
                for sample in self._samples
            ]
        return result


def _finite_float(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _positive_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def normalize_sample(value: Any, *, timestamp: float | None = None) -> ProcessSample | None:
    """Normalize a source return value into :class:`ProcessSample`.

    Unknown/malformed samples are ignored rather than raising.  This is
    important for optional observability: one transient ``ps`` parse failure
    should not abort an otherwise valid benchmark.
    """

    if value is None:
        return None
    if isinstance(value, ProcessSample):
        sample_timestamp = value.timestamp if value.timestamp is not None else timestamp
        cpu = _finite_float(value.cpu_percent)
        rss = _positive_int(value.rss_bytes)
        if cpu is None and rss is None:
            return None
        return ProcessSample(
            cpu_percent=cpu,
            rss_bytes=rss,
            timestamp=_finite_float(sample_timestamp),
        )
    cpu: Any = None
    rss: Any = None
    sample_timestamp: Any = timestamp
    if isinstance(value, Mapping):
        # Accept common spellings while retaining explicit units as the
        # preferred form.
        for key in (
            "cpu_percent", "cpu_pct", "cpu", "percent_cpu", "cpu_utilization",
        ):
            if key in value:
                cpu = value[key]
                break
        for key in (
            "rss_bytes", "resident_bytes", "rss", "resident_set_size",
        ):
            if key in value:
                rss = value[key]
                break
        if "timestamp" in value:
            sample_timestamp = value["timestamp"]
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        cpu, rss = value[0], value[1]
        if len(value) >= 3:
            sample_timestamp = value[2]
    else:
        return None
    normalized_timestamp = _finite_float(sample_timestamp)
    normalized_cpu = _finite_float(cpu)
    normalized_rss = _positive_int(rss)
    # Do not count a completely malformed source result as an observation.
    # A partially valid sample is still useful (for example CPU-only data on
    # a platform where RSS is unavailable).
    if normalized_cpu is None and normalized_rss is None:
        return None
    return ProcessSample(
        cpu_percent=normalized_cpu,
        rss_bytes=normalized_rss,
        timestamp=normalized_timestamp,
    )


def _psutil_source() -> ProcessSampleSource | None:
    """Build a psutil source if the optional package is installed."""

    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        return None

    processes: dict[int, Any] = {}
    lock = threading.Lock()

    def read(pid: int) -> ProcessSample:
        with lock:
            process = processes.get(pid)
            if process is None:
                process = psutil.Process(pid)
                processes[pid] = process
        # ``cpu_percent(None)`` measures since the previous call and is
        # suitable for periodic sampling.  The first value is commonly zero,
        # which is a valid sample and is retained for transparent accounting.
        cpu = process.cpu_percent(interval=None)
        memory = process.memory_info().rss
        return ProcessSample(cpu_percent=cpu, rss_bytes=memory, timestamp=time.monotonic())

    return read


def _ps_source() -> ProcessSampleSource | None:
    """Build a safe ``ps`` source for macOS/Linux.

    The command requests only numeric CPU and RSS columns by PID; it never
    asks for ``command``, ``args`` or environment fields.
    """

    if platform.system().lower() not in {"darwin", "linux"}:
        return None

    def read(pid: int) -> ProcessSample:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu=,rss="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if completed.returncode != 0:
            raise ProcessLookupError(f"process {pid} is not available")
        line = next((item.strip() for item in completed.stdout.splitlines() if item.strip()), "")
        fields = line.split()
        if len(fields) < 2:
            raise ProcessLookupError(f"process {pid} is not available")
        cpu = _finite_float(fields[0])
        rss_kib = _finite_float(fields[1])
        if cpu is None or rss_kib is None or rss_kib < 0:
            raise ValueError("ps returned malformed CPU/RSS values")
        return ProcessSample(
            cpu_percent=cpu,
            rss_bytes=int(round(rss_kib * 1024)),
            timestamp=time.monotonic(),
        )

    return read


def default_sample_source() -> ProcessSampleSource | None:
    """Return the best available source without raising on unsupported hosts."""

    source = _psutil_source()
    return source or _ps_source()


class ProcessSampler:
    """Periodically sample a process, with synchronous and start/stop APIs.

    Parameters are intentionally injectable so unit tests can use a fake
    source, clock and sleep function without spawning a process or waiting in
    real time.  ``start`` launches a daemon thread; ``collect`` is a compact
    synchronous helper for scripts.
    """

    def __init__(
        self,
        pid: int,
        *,
        interval: float = 1.0,
        source: ProcessSampleSource | Callable[[int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            normalized_pid = int(pid)
        except (TypeError, ValueError) as exc:
            raise ValueError("pid must be an integer") from exc
        if normalized_pid <= 0:
            raise ValueError("pid must be positive")
        try:
            normalized_interval = float(interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval must be a finite non-negative number") from exc
        if not math.isfinite(normalized_interval) or normalized_interval < 0:
            raise ValueError("interval must be a finite non-negative number")
        self.pid = normalized_pid
        self.interval = normalized_interval
        self.source = source if source is not None else default_sample_source()
        self.clock = clock
        self.sleep = sleep
        self._samples: list[ProcessSample] = []
        self._status = "ok" if self.source is not None else "unavailable"
        self._reason: str | None = None if self.source is not None else "process sampling is unsupported on this platform"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopped_at: float | None = None

    def _reset_window(self) -> None:
        """Clear samples and transient status for a new measurement window."""

        with self._lock:
            self._samples.clear()
        self._status = "ok" if self.source is not None else "unavailable"
        self._reason = (
            None
            if self.source is not None
            else "process sampling is unsupported on this platform"
        )

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def status(self) -> str:
        return self._status

    @property
    def reason(self) -> str | None:
        return self._reason

    def _record_failure(self, exc: BaseException) -> None:
        # A missing/permission-denied PID is an expected optional condition;
        # classify it as unavailable.  Unexpected parser/source failures are
        # still non-fatal but are surfaced as ``error`` for diagnosis.
        if (
            isinstance(exc, (ProcessLookupError, FileNotFoundError, PermissionError, NotImplementedError))
            or type(exc).__name__ in {"NoSuchProcess", "ZombieProcess", "AccessDenied"}
        ):
            self._status = "unavailable"
            # Exception text can contain command lines, URLs, or credentials;
            # expose only the stable type for diagnostics.
            self._reason = f"process unavailable ({type(exc).__name__})"
        else:
            self._status = "error"
            self._reason = f"process sampling failed ({type(exc).__name__})"

    def sample_once(self) -> ProcessSample | None:
        """Read and record one sample; return ``None`` when unavailable."""

        source = self.source
        if source is None:
            self._status = "unavailable"
            self._reason = self._reason or "process sampling is unsupported on this platform"
            return None
        try:
            # Keep even an injected clock inside the best-effort boundary;
            # custom clocks can fail just like process readers.
            timestamp = self.clock()
            if callable(source):
                value = source(self.pid)
            elif hasattr(source, "sample"):
                value = source.sample(self.pid)  # type: ignore[attr-defined]
            else:
                raise TypeError("sample source must be callable or expose sample(pid)")
        except BaseException as exc:  # source is optional instrumentation; never crash caller
            self._record_failure(exc)
            return None
        sample = normalize_sample(value, timestamp=timestamp)
        if sample is None:
            # A source returning None is commonly how psutil fakes signal a
            # vanished process.  Keep a clear status rather than pretending a
            # zero-valued sample was observed.
            self._status = "unavailable"
            self._reason = self._reason or f"process {self.pid} returned no sample"
            return None
        with self._lock:
            self._samples.append(sample)
        self._status = "ok"
        self._reason = None
        return sample

    def _loop(self) -> None:
        # Capture immediately, then wait between subsequent samples.  This
        # means short benchmark rounds still have at least one observation.
        self.sample_once()
        while not self._stop_event.wait(self.interval):
            self.sample_once()

    def start(self) -> "ProcessSampler":
        """Start background sampling and return ``self`` for fluent use."""

        if self.running:
            return self
        if self.interval <= 0:
            # Event.wait(0) would create a hot loop and consume an entire CPU.
            # Zero remains useful for deterministic synchronous ``collect``
            # calls, but is never accepted for a background sampler.
            raise ValueError("interval must be positive for background sampling")
        self._reset_window()
        self._stop_event.clear()
        self._started_at = self.clock()
        self._stopped_at = None
        self._thread = threading.Thread(
            target=self._loop,
            name=f"process-sampler-{self.pid}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, *, join_timeout: float | None = 5.0) -> ResourceStats:
        """Stop a background sampler and return aggregate statistics."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(join_timeout)
        self._stopped_at = self.clock()
        return self.summary()

    def collect(self, *, duration: float | None = None, samples: int | None = None) -> ResourceStats:
        """Collect synchronously for ``duration`` or a fixed sample count.

        At least one of ``duration`` and ``samples`` may be omitted; with both
        omitted a single sample is taken.  This method is deterministic with a
        fake ``sleep``/``clock`` and is convenient for tests.
        """

        if duration is not None:
            try:
                duration_value = float(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError("duration must be a finite non-negative number") from exc
            if not math.isfinite(duration_value) or duration_value < 0:
                raise ValueError("duration must be a finite non-negative number")
        else:
            duration_value = None
        if samples is not None:
            try:
                sample_limit = int(samples)
            except (TypeError, ValueError) as exc:
                raise ValueError("samples must be a non-negative integer") from exc
            if sample_limit < 0:
                raise ValueError("samples must be a non-negative integer")
        else:
            sample_limit = None

        self._reset_window()
        try:
            started = self.clock()
        except BaseException as exc:
            self._record_failure(exc)
            self._stopped_at = None
            return self.summary()
        self._started_at = started
        count = 0
        if sample_limit == 0:
            self._status = "unavailable"
            self._reason = "no samples requested"
            try:
                self._stopped_at = self.clock()
            except BaseException as exc:
                self._record_failure(exc)
                self._stopped_at = None
            return self.summary()
        while True:
            self.sample_once()
            count += 1
            try:
                elapsed = max(0.0, self.clock() - started)
            except BaseException as exc:
                self._record_failure(exc)
                break
            if sample_limit is not None and count >= sample_limit:
                break
            if duration_value is not None and elapsed >= duration_value:
                break
            if sample_limit is None and duration_value is None:
                break
            # Avoid an accidental infinite loop with a zero-increment fake
            # clock: a zero interval still permits progress by taking samples
            # until the requested count, while duration-only callers get one
            # sample when the clock cannot advance.
            wait_for = self.interval
            if duration_value is not None:
                wait_for = min(wait_for, max(0.0, duration_value - elapsed))
            if wait_for <= 0 and duration_value is not None:
                break
            self.sleep(wait_for)
        try:
            self._stopped_at = self.clock()
        except BaseException as exc:
            self._record_failure(exc)
            self._stopped_at = None
        return self.summary()

    def summary(self) -> ResourceStats:
        """Compute an immutable-style aggregate from samples observed so far."""

        with self._lock:
            samples = list(self._samples)
        cpus = [sample.cpu_percent for sample in samples if sample.cpu_percent is not None]
        rss_values = [sample.rss_bytes for sample in samples if sample.rss_bytes is not None]
        status = self._status
        if samples and status == "unavailable":
            # A process can disappear after valid observations; retain the
            # useful aggregate while explaining the terminal state.
            status = "ok"
        duration = None
        if self._started_at is not None and self._stopped_at is not None:
            duration = max(0.0, self._stopped_at - self._started_at)
        return ResourceStats(
            pid=self.pid,
            status=status,
            reason=self._reason,
            sample_count=len(samples),
            interval_seconds=self.interval,
            duration_seconds=duration,
            cpu_percent_avg=(sum(cpus) / len(cpus)) if cpus else None,
            cpu_percent_max=max(cpus) if cpus else None,
            rss_bytes_avg=(sum(rss_values) / len(rss_values)) if rss_values else None,
            rss_bytes_peak=max(rss_values) if rss_values else None,
            _samples=samples,
        )

    def __enter__(self) -> "ProcessSampler":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


# A concise alias for callers that prefer ``ResourceSampler`` terminology.
ResourceSampler = ProcessSampler


def sample_process(
    pid: int,
    *,
    duration: float | None = None,
    interval: float = 1.0,
    samples: int | None = None,
    source: ProcessSampleSource | Callable[[int], Any] | None = None,
) -> ResourceStats:
    """Convenience wrapper for one synchronous sampling window."""

    return ProcessSampler(pid, interval=interval, source=source).collect(
        duration=duration,
        samples=samples,
    )


sample_resources = sample_process


def render_markdown(stats: ResourceStats) -> str:
    """Render a small, secret-free resource summary."""

    data = stats.to_dict()
    reason = str(data["reason"] or "n/a")
    reason = " ".join(
        ch if (ch.isprintable() or ch in "\t ") else " "
        for ch in reason
    ).replace("`", "'")
    reason = " ".join(reason.split())[:200]
    lines = [
        "# Process resource sample",
        "",
        f"- PID: `{data['pid']}`",
        f"- Status: `{data['status']}`",
        f"- Reason: `{reason}`",
        f"- Samples: `{data['sample_count']}`",
        f"- Interval: `{data['interval_seconds']}` seconds",
        f"- CPU average/max: `{data['cpu_percent_avg']}` / `{data['cpu_percent_max']}` percent",
        f"- RSS average/peak: `{data['rss_bytes_avg']}` / `{data['rss_bytes_peak']}` bytes",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optionally sample CPU/RSS for a target process")
    parser.add_argument("--pid", type=int, required=True, help="Target process ID")
    parser.add_argument("--interval", "--sample-interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=None, help="Sampling duration in seconds")
    parser.add_argument("--samples", type=int, default=None, help="Stop after this many samples")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, default=None, help="Write output to a file instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.duration is not None and args.duration < 0:
            parser.error("--duration must be non-negative")
        if args.samples is not None and args.samples < 0:
            parser.error("--samples must be non-negative")
        sampler = ProcessSampler(args.pid, interval=args.interval)
        stats = sampler.collect(duration=args.duration, samples=args.samples)
        if args.format == "markdown":
            rendered = render_markdown(stats)
        else:
            import json

            rendered = json.dumps(stats.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="" if rendered.endswith("\n") else "\n")
        # Unavailable is an explicit, successful optional-measurement result;
        # callers can inspect ``status`` without handling a traceback.
        return 0
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130
    return 2  # pragma: no cover - argparse.error exits before this point


__all__ = [
    "ProcessSample",
    "ProcessSampleSource",
    "ProcessSampler",
    "ResourceSampler",
    "ResourceStats",
    "default_sample_source",
    "normalize_sample",
    "sample_process",
    "sample_resources",
    "render_markdown",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
