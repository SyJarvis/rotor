"""Compare two independent benchmark rounds.

The command compares a direct-provider round (baseline) with a gateway round
and reports the gateway-minus-direct delta.  It accepts either a result
directory produced by :mod:`benchmarks.runner` or a standalone ``summary.json``
file.  Scenario metadata is checked before metrics are interpreted; a
conflicting protocol, stream mode, model, prompt fingerprint, or load setting
is reported explicitly and the CLI exits non-zero unless the caller opts into
``--allow-incomparable``.

Only a small, safe whitelist of metadata is emitted.  In particular, request
rows, response text, prompts, headers, API keys, and arbitrary environment
values are never copied into the comparison artifact.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .metrics import RequestResult, summarize_results
from .report import redact_api_key, redact_url


class CompareError(ValueError):
    """Raised when a result artifact cannot be read or interpreted safely."""


class IncomparableRunsError(CompareError):
    """Raised by callers that request strict comparability."""


@dataclass(frozen=True)
class RoundArtifacts:
    """Loaded, normalized artifacts for one benchmark round."""

    source: Path
    environment: Mapping[str, Any]
    summary: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.name,
            "environment": dict(self.environment),
            "summary": dict(self.summary),
            "rows": [dict(row) for row in self.rows],
        }


# Values that define whether two rounds answer the same experiment.  Target
# URL and API key are deliberately absent: direct and gateway targets must
# differ by design.  ``duration``/``elapsed`` are measurement outcomes rather
# than scenario controls and are therefore not comparability keys.
SCENARIO_KEYS: tuple[str, ...] = (
    "protocol",
    "stream",
    "model",
    "concurrency",
    "requests",
    "warmup",
    "timeout",
    "rate",
    "max_tokens",
    "prompt_chars",
    "prompt_sha256",
    "concurrency_model",
    "conversation_mode",
    "rounds",
    "duration_seconds_requested",
)

# Strict comparison needs enough identity to establish that both artifacts
# describe the same experiment, but should remain usable with compact
# hand-written summaries.  Optional controls (rate, timeout, prompt length,
# etc.) are still checked whenever present; they are not required solely for
# strict verification.  A finite workload records ``requests`` while a
# duration workload records ``duration_seconds_requested``.
SCENARIO_REQUIRED_KEYS: tuple[str, ...] = (
    "protocol",
    "stream",
    "model",
    "concurrency",
)
SCENARIO_WORKLOAD_KEYS: tuple[str, ...] = (
    "requests",
    "duration_seconds_requested",
)

_INVALID_SCENARIO_VALUE = "<invalid-scenario-value>"
_SUPPORTED_PROTOCOLS = {"chat", "responses", "anthropic"}

_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(api[_-]?key|token|secret|password|authorization|credential|dsn)(?:$|[_-])",
    re.IGNORECASE,
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompareError(f"benchmark artifact does not exist: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise CompareError(f"unable to read benchmark artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CompareError(f"invalid JSON in benchmark artifact {path}: line {exc.lineno} column {exc.colno}") from exc


def _read_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.exists():
        return ()
    rows: list[Mapping[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CompareError(f"unable to read benchmark raw results: {path}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CompareError(f"invalid JSON in {path} at line {line_number}") from exc
        if not isinstance(value, Mapping):
            raise CompareError(f"raw result at {path}:{line_number} is not an object")
        rows.append(dict(value))
    return tuple(rows)


def _summary_from_file(value: Any, path: Path) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompareError(f"summary artifact is not a JSON object: {path}")
    # A few callers wrap summaries in {"summary": ...}; accepting that shape
    # costs nothing and makes the CLI useful with exported report bundles.
    nested = value.get("summary")
    if isinstance(nested, Mapping):
        return dict(nested)
    return dict(value)


def _summary_needs_rows(summary: Mapping[str, Any]) -> bool:
    """Return whether row-level derivation is needed for a sparse summary.

    A completed runner summary already contains totals and the metric groups
    consumed by comparison.  In that common case reading a potentially
    million-line duration JSONL would only waste memory and could fail before
    comparison starts.  Sparse/hand-written summaries retain the historical
    row-derivation behavior.
    """

    if "total_requests" not in summary:
        return True
    if not any(
        key in summary
        for key in ("rps", "latency_ms", "latency_p50_ms", "ttft_ms", "ttft_p50_ms")
    ):
        return True
    return False


def load_round(source: str | Path) -> RoundArtifacts:
    """Load a result directory, summary JSON, or raw-results JSONL file.

    Missing ``environment.json`` is allowed for hand-written summaries; the
    comparison then marks scenario identity as unverified rather than
    inventing metadata.  A directory with neither summary nor raw rows fails
    clearly.
    """

    requested = Path(source)
    if not requested.exists():
        raise CompareError(f"benchmark result path does not exist: {requested}")
    if requested.is_dir():
        root = requested
        summary_path = root / "summary.json"
        raw_path = root / "raw-results.jsonl"
        environment_path = root / "environment.json"
        environment: Mapping[str, Any] = {}
        if environment_path.exists():
            value = _read_json(environment_path)
            if not isinstance(value, Mapping):
                raise CompareError(f"environment artifact is not a JSON object: {environment_path}")
            environment = dict(value)
        summary: Mapping[str, Any]
        if summary_path.exists():
            summary = _summary_from_file(_read_json(summary_path), summary_path)
        else:
            rows = _read_rows(raw_path)
            if not rows:
                raise CompareError(f"result directory has no summary.json or raw-results.jsonl rows: {root}")
            summary = summarize_results(rows)
            return RoundArtifacts(root, environment, summary, rows)
        # Avoid loading raw rows when the persisted summary is complete.  This
        # is especially important for duration runs, whose JSONL can be much
        # larger than the bounded in-memory quantile sample.
        rows = _read_rows(raw_path) if _summary_needs_rows(summary) else ()
        # Fill only absent summary sections from rows.  This preserves an
        # explicitly recorded summary while making sparse hand-written files
        # comparable without silently replacing values.
        if rows:
            derived = summarize_results(rows)
            summary = _merge_missing_summary(summary, derived)
        return RoundArtifacts(root, environment, summary, rows)

    if not requested.is_file():
        raise CompareError(f"benchmark result path is not a regular file: {requested}")
    if requested.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = _read_rows(requested)
        if not rows:
            raise CompareError(f"raw result file is empty: {requested}")
        environment: Mapping[str, Any] = {}
        adjacent = requested.parent / "environment.json"
        if adjacent.exists():
            value = _read_json(adjacent)
            if isinstance(value, Mapping):
                environment = dict(value)
        return RoundArtifacts(requested, environment, summarize_results(rows), rows)

    value = _read_json(requested)
    if not isinstance(value, Mapping):
        raise CompareError(f"benchmark JSON artifact is not an object: {requested}")
    environment: Mapping[str, Any] = {}
    summary_value: Any = value
    # A bundle may contain both environment and summary in one JSON file.
    if isinstance(value.get("environment"), Mapping):
        environment = dict(value["environment"])
    if isinstance(value.get("summary"), Mapping):
        summary_value = value["summary"]
    elif requested.name != "summary.json" and isinstance(value.get("metrics"), Mapping):
        # Preserve compatibility with an earlier comparison export; its
        # summary fields are not needed, but treating it as a summary gives a
        # useful error/empty metric rather than crashing on an unfamiliar key.
        summary_value = value
    summary = _summary_from_file(summary_value, requested)
    adjacent_environment = requested.parent / "environment.json"
    if not environment and adjacent_environment.exists():
        adjacent = _read_json(adjacent_environment)
        if isinstance(adjacent, Mapping):
            environment = dict(adjacent)
    return RoundArtifacts(requested, environment, summary, ())


def _merge_missing_summary(primary: Mapping[str, Any], derived: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in derived.items():
        if key not in merged or merged[key] is None:
            merged[key] = value
        elif isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            child = dict(merged[key])
            for child_key, child_value in value.items():
                if child_key not in child or child[child_key] is None:
                    child[child_key] = child_value
            merged[key] = child
    return merged


def _coerce_round(value: RoundArtifacts | str | Path | Mapping[str, Any]) -> RoundArtifacts:
    if isinstance(value, RoundArtifacts):
        return value
    if isinstance(value, (str, Path)):
        return load_round(value)
    if isinstance(value, Mapping):
        # Programmatic callers may pass {environment, summary, rows} or a
        # plain summary mapping.  Never mutate their object.
        environment = value.get("environment") if isinstance(value.get("environment"), Mapping) else {}
        summary_value = value.get("summary") if isinstance(value.get("summary"), Mapping) else value
        rows_value = value.get("rows")
        rows: tuple[Mapping[str, Any], ...] = ()
        if isinstance(rows_value, Iterable) and not isinstance(rows_value, (str, bytes, Mapping)):
            rows = tuple(item for item in rows_value if isinstance(item, Mapping))
        summary = dict(summary_value) if isinstance(summary_value, Mapping) else {}
        if rows:
            summary = _merge_missing_summary(summary, summarize_results(rows))
        return RoundArtifacts(Path("<memory>"), dict(environment), summary, rows)
    raise CompareError(f"unsupported benchmark result value: {type(value).__name__}")


def _normalise_protocol(value: Any) -> Any:
    if value is None:
        return None
    aliases = {
        "openai": "chat",
        "openai_chat": "chat",
        "chat_completions": "chat",
        "openai_responses": "responses",
        "response": "responses",
        "anthropic_messages": "anthropic",
        "messages": "anthropic",
    }
    text = str(value).strip().lower().replace("-", "_")
    return aliases.get(text, text)


def _normalise_scenario_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key == "protocol":
        return _normalise_protocol(value)
    if key == "stream":
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            # Do not silently coerce arbitrary labels (e.g. ``"maybe"``)
            # to False; retain an explicit marker so even default comparison
            # reports the malformed control instead of treating equal junk as
            # verified.
            return _INVALID_SCENARIO_VALUE
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and value in {0, 1}:
            return bool(value)
        return _INVALID_SCENARIO_VALUE
    if key in {"concurrency", "requests", "warmup", "max_tokens", "prompt_chars", "rounds"}:
        if isinstance(value, bool):
            return _INVALID_SCENARIO_VALUE
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"[+-]?\d+", text):
                try:
                    return int(text)
                except ValueError:
                    return _INVALID_SCENARIO_VALUE
            return _INVALID_SCENARIO_VALUE
        try:
            number = float(value)
            if math.isfinite(number) and number.is_integer():
                return int(number)
        except (TypeError, ValueError):
            pass
        return _INVALID_SCENARIO_VALUE
    if key in {"timeout", "rate", "duration_seconds_requested"}:
        try:
            number = float(value)
            if math.isfinite(number):
                # Treat runner's explicit zero rate and an omitted rate as the
                # same launch policy.
                return None if key == "rate" and number == 0 else number
        except (TypeError, ValueError):
            pass
    return str(value) if isinstance(value, Path) else value


def _invalid_scenario_fields(values: Mapping[str, Any]) -> set[str]:
    """Identify malformed controls before scenario equality is interpreted."""

    invalid: set[str] = set()
    protocol = values.get("protocol")
    if protocol is not None and protocol not in _SUPPORTED_PROTOCOLS:
        invalid.add("protocol")
    stream = values.get("stream")
    if stream is not None and not isinstance(stream, bool):
        invalid.add("stream")
    model = values.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        invalid.add("model")
    for key in ("concurrency", "requests", "max_tokens", "rounds"):
        if key in values:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                invalid.add(key)
    if "warmup" in values:
        value = values["warmup"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            invalid.add("warmup")
    for key in ("timeout", "duration_seconds_requested"):
        if key in values:
            try:
                number = float(values[key])
            except (TypeError, ValueError):
                number = math.nan
            if not math.isfinite(number) or number <= 0:
                invalid.add(key)
    if "rate" in values and values["rate"] is not None:
        try:
            number = float(values["rate"])
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number) or number < 0:
            invalid.add("rate")
    if "prompt_chars" in values:
        value = values["prompt_chars"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            invalid.add("prompt_chars")
    if "prompt_sha256" in values:
        value = values["prompt_sha256"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            invalid.add("prompt_sha256")
    if "conversation_mode" in values and values["conversation_mode"] not in {"fixed", "random"}:
        invalid.add("conversation_mode")
    return invalid


def _scenario_map(round_data: RoundArtifacts) -> dict[str, Any]:
    env = round_data.environment
    params: Mapping[str, Any] = {}
    if isinstance(env.get("parameters"), Mapping):
        params = env["parameters"]
    elif isinstance(env.get("scenario"), Mapping):
        params = env["scenario"]
    # Hand-written summaries sometimes carry controls at the top level.
    summary = round_data.summary
    result: dict[str, Any] = {}
    for key in SCENARIO_KEYS:
        candidates = [key]
        if key == "model":
            candidates.extend(("requested_model", "provider_model"))
        elif key == "duration_seconds_requested":
            candidates.extend(
                (
                    "duration_seconds",
                    "duration",
                    "duration_target_seconds",
                    "target_duration_seconds",
                )
            )
        value: Any = None
        found = False
        for candidate in candidates:
            if candidate in params:
                value, found = params[candidate], True
                break
            if candidate in env:
                value, found = env[candidate], True
                break
            if candidate in summary:
                value, found = summary[candidate], True
                break
        if found and value is not None:
            normalized = _normalise_scenario_value(key, value)
            if normalized is not None:
                result[key] = normalized

    # Rows provide a safe fallback for protocol/stream when no environment was
    # persisted.  Request count is inferred only when raw rows are present.
    if round_data.rows:
        protocols = {_normalise_protocol(row.get("protocol")) for row in round_data.rows if row.get("protocol") is not None}
        streams = {
            _normalise_scenario_value("stream", row.get("stream"))
            for row in round_data.rows
            if "stream" in row
        }
        streams.discard(None)
        if "protocol" not in result and len(protocols) == 1:
            result["protocol"] = next(iter(protocols))
        if "stream" not in result and len(streams) == 1:
            result["stream"] = next(iter(streams))
        if "requests" not in result:
            result["requests"] = len(round_data.rows)
    return result


def compare_scenarios(
    direct: RoundArtifacts | str | Path | Mapping[str, Any],
    gateway: RoundArtifacts | str | Path | Mapping[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Compare scenario controls and return explicit mismatch diagnostics."""

    direct_round = _coerce_round(direct)
    gateway_round = _coerce_round(gateway)
    direct_values = _scenario_map(direct_round)
    gateway_values = _scenario_map(gateway_round)
    direct_invalid = _invalid_scenario_fields(direct_values)
    gateway_invalid = _invalid_scenario_fields(gateway_values)
    checked: dict[str, Any] = {}
    mismatches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    # Duration is the workload control in duration mode.  Older artifacts may
    # still contain an arbitrary ``requests`` placeholder; never compare that
    # placeholder when both rounds explicitly record a duration.
    duration_mode = (
        "duration_seconds_requested" in direct_values
        and "duration_seconds_requested" in gateway_values
    )
    if duration_mode:
        # Placeholder request counts are outside the duration workload
        # identity, including malformed/zero legacy placeholders.
        direct_invalid.discard("requests")
        gateway_invalid.discard("requests")
    for key in SCENARIO_KEYS:
        if key == "requests" and duration_mode:
            continue
        left_present = key in direct_values
        right_present = key in gateway_values
        if not left_present and not right_present:
            continue
        if not left_present or not right_present:
            item = {
                "parameter": key,
                "direct": direct_values.get(key),
                "gateway": gateway_values.get(key),
                "reason": "missing from one round",
            }
            missing.append(item)
            mismatches.append(item)
            continue
        left, right = direct_values[key], gateway_values[key]
        checked[key] = {"direct": left, "gateway": right}
        if left != right:
            mismatches.append({
                "parameter": key,
                "direct": left,
                "gateway": right,
                "reason": "scenario values differ",
            })
    for key in sorted(direct_invalid | gateway_invalid):
        # Invalid controls are never considered a valid equality, even when
        # both hand-written artifacts happen to contain the same malformed
        # value.  Keep the raw value only long enough to produce diagnostics;
        # the returned structure is sanitized below.
        if not any(item.get("parameter") == key for item in mismatches):
            mismatches.append({
                "parameter": key,
                "direct": direct_values.get(key),
                "gateway": gateway_values.get(key),
                "reason": "invalid scenario value",
            })
    # ``verified`` describes metadata completeness, not merely whether one
    # convenient field happened to match.  This remains false in default mode
    # (callers may still inspect/report metrics), while strict mode additionally
    # makes each missing required control an explicit incompatibility.
    missing_required: list[str] = []
    for key in SCENARIO_REQUIRED_KEYS:
        if key not in direct_values or key not in gateway_values:
            missing_required.append(key)
    if not any(key in direct_values and key in gateway_values for key in SCENARIO_WORKLOAD_KEYS):
        missing_required.append("requests_or_duration")
    verified = bool(checked) and not missing_required and not missing and not (direct_invalid | gateway_invalid)
    if strict and missing_required:
        for key in missing_required:
            if key == "requests_or_duration":
                direct_value = direct_values.get("requests", direct_values.get("duration_seconds_requested"))
                gateway_value = gateway_values.get("requests", gateway_values.get("duration_seconds_requested"))
            else:
                direct_value = direct_values.get(key)
                gateway_value = gateway_values.get(key)
            mismatches.append({
                "parameter": key,
                "direct": direct_value,
                "gateway": gateway_value,
                "reason": "required scenario metadata missing for strict comparison",
            })

    def safe_value(key: str, value: Any) -> Any:
        # _safe_scalar intentionally accepts only scalar output.  Scenario
        # values are normalized scalars; a malformed mapping/list becomes a
        # bounded marker instead of leaking nested secrets.
        return _safe_scalar(key, value)

    safe_checked = {
        key: {
            "direct": safe_value(key, value.get("direct")),
            "gateway": safe_value(key, value.get("gateway")),
        }
        for key, value in checked.items()
        if isinstance(value, Mapping)
    }
    safe_mismatches = [
        {
            "parameter": safe_value("parameter", item.get("parameter")),
            "direct": safe_value(str(item.get("parameter", "scenario")), item.get("direct")),
            "gateway": safe_value(str(item.get("parameter", "scenario")), item.get("gateway")),
            "reason": safe_value("reason", item.get("reason")),
        }
        for item in mismatches
        if isinstance(item, Mapping)
    ]
    safe_missing = [
        {
            "parameter": safe_value("parameter", item.get("parameter")),
            "direct": safe_value(str(item.get("parameter", "scenario")), item.get("direct")),
            "gateway": safe_value(str(item.get("parameter", "scenario")), item.get("gateway")),
            "reason": safe_value("reason", item.get("reason")),
        }
        for item in missing
        if isinstance(item, Mapping)
    ]
    return {
        "comparable": not mismatches,
        "verified": verified and not missing,
        "checked": safe_checked,
        "missing": safe_missing,
        "mismatches": safe_mismatches,
        "direct": {key: safe_value(key, value) for key, value in direct_values.items()},
        "gateway": {key: safe_value(key, value) for key, value in gateway_values.items()},
    }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nested_number(summary: Mapping[str, Any], group: str, key: str, aliases: Sequence[str] = ()) -> float | None:
    nested = summary.get(group)
    if isinstance(nested, Mapping):
        for candidate in (key, *aliases):
            if candidate in nested:
                number = _finite_number(nested[candidate])
                if number is not None:
                    return number
    for candidate in (f"{group}_{key}", *aliases):
        if candidate in summary:
            number = _finite_number(summary[candidate])
            if number is not None:
                return number
    return None


def _metric_value(summary: Mapping[str, Any], metric: str) -> float | None:
    if metric in {"latency_p50_ms", "latency_p95_ms", "latency_p99_ms"}:
        # Prefer the explicit flat alias when present, then fall back to the
        # nested ``latency_ms: {p50: ...}`` form used by older artifacts.
        direct = _finite_number(summary.get(metric))
        if direct is not None:
            return direct
        key = metric.removeprefix("latency_").removesuffix("_ms")
        return _nested_number(summary, "latency_ms", key)
    if metric in {"ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms"}:
        direct = _finite_number(summary.get(metric))
        if direct is not None:
            return direct
        key = metric.removeprefix("ttft_").removesuffix("_ms")
        return _nested_number(summary, "ttft_ms", key)
    aliases = {
        "rps": ("requests_per_second",),
        "successful_rps": ("success_rps", "successful_requests_per_second"),
        "success_rate": (),
        "error_rate": (),
    }
    for candidate in (metric, *aliases.get(metric, ())):
        if candidate in summary:
            number = _finite_number(summary[candidate])
            if number is not None:
                return number
    if metric == "success_rate":
        total = _finite_number(summary.get("total_requests"))
        successful = _finite_number(summary.get("successful_requests"))
        if total and total > 0 and successful is not None:
            return successful / total
    if metric == "error_rate":
        total = _finite_number(summary.get("total_requests"))
        failed = _finite_number(summary.get("failed_requests"))
        if total and total > 0 and failed is not None:
            return failed / total
    return None


def _usage_value(summary: Mapping[str, Any], key: str) -> float | None:
    usage = summary.get("usage")
    aliases = {
        "input_tokens": ("prompt_tokens", "input"),
        "output_tokens": ("completion_tokens", "output"),
        "total_tokens": ("total",),
        "requests_with_usage": (),
    }
    if isinstance(usage, Mapping):
        for candidate in (key, *aliases.get(key, ())):
            if candidate in usage:
                number = _finite_number(usage[candidate])
                if number is not None:
                    return number
    for candidate in (key, *aliases.get(key, ())):
        if candidate in summary:
            number = _finite_number(summary[candidate])
            if number is not None:
                return number
    return None


def _entry(direct: float | None, gateway: float | None) -> dict[str, float | None]:
    delta: float | None = None
    if direct is not None and gateway is not None:
        try:
            delta = _finite_number(gateway - direct)
        except (OverflowError, ValueError):
            delta = None
    relative = None
    if delta is not None and direct not in (None, 0):
        try:
            relative = _finite_number(delta / direct * 100.0)
        except (OverflowError, ZeroDivisionError, ValueError):
            relative = None
    return {
        "direct": direct,
        "gateway": gateway,
        "delta": delta,
        "relative_delta_percent": relative,
    }


def _build_metrics(direct: RoundArtifacts, gateway: RoundArtifacts) -> dict[str, Any]:
    metric_names = (
        "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
        "ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms",
        "rps", "successful_rps", "success_rate", "error_rate",
    )
    flat: dict[str, Any] = {
        name: _entry(_metric_value(direct.summary, name), _metric_value(gateway.summary, name))
        for name in metric_names
    }
    latency = {name.removeprefix("latency_"): flat[name] for name in metric_names if name.startswith("latency_")}
    ttft = {name.removeprefix("ttft_"): flat[name] for name in metric_names if name.startswith("ttft_")}
    throughput = {name: flat[name] for name in ("rps", "successful_rps")}
    rates = {name: flat[name] for name in ("success_rate", "error_rate")}
    usage: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "requests_with_usage"):
        usage[key] = _entry(_usage_value(direct.summary, key), _usage_value(gateway.summary, key))
    result: dict[str, Any] = {
        "latency_ms": latency,
        "ttft_ms": ttft,
        "throughput": throughput,
        "rates": rates,
        "usage": usage,
        # Flat aliases make shell/JQ consumers convenient and preserve the
        # exact names used by summary.json.
        **flat,
        "usage_delta": {
            key: value["delta"] for key, value in usage.items()
        },
        "usage_difference": {
            key: value["delta"] for key, value in usage.items()
        },
    }
    return result


def _safe_scalar(key: str, value: Any) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # Scenario/summary values are expected to be scalars.  Never stringify a
    # nested object here: its repr could contain an untrusted secret or body.
    if isinstance(value, (Mapping, list, tuple, set)):
        return "<non-scalar>"
    text = str(value)
    if _SECRET_KEY_RE.search(key):
        return redact_api_key(text)
    if "url" in key.lower() or "://" in text:
        return redact_url(text)
    # Avoid allowing arbitrary input to inject Markdown/control characters.
    # Keep a bounded, single-line label for human-readable reports.
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).replace("`", "'")
    return " ".join(text.split())[:200]


def _safe_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist summary fields and known nested metric/counter keys.

    Summary artifacts are often assembled by scripts, so nested mappings must
    be treated as untrusted too.  Only the fields emitted by the benchmark
    metrics contract are copied; arbitrary labels (for example ``prompt`` or
    ``secret``) are dropped rather than stringified.
    """

    allowed = {
        "total_requests", "successful_requests", "failed_requests", "success_rate",
        "error_rate", "elapsed_seconds", "rps", "successful_rps", "status_codes",
        "latency_ms", "ttft_ms", "usage", "output_tokens_per_s", "chunk_count",
    }
    nested_allowed: dict[str, set[str] | None] = {
        "latency_ms": {"count", "p50", "p90", "p95", "p99", "max", "max_round_p99", "min", "mean", "stdev", "stddev", "range"},
        "ttft_ms": {"count", "p50", "p90", "p95", "p99", "max", "max_round_p99", "min", "mean", "stdev", "stddev", "range"},
        "usage": {"input_tokens", "output_tokens", "total_tokens", "requests_with_usage"},
        # HTTP status labels are dynamic but constrained to numeric status
        # codes (plus the explicit ``none`` bucket used by the runner).
        "status_codes": None,
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in summary:
            continue
        value = summary[key]
        if isinstance(value, Mapping):
            permitted = nested_allowed.get(key, set())
            child: dict[str, Any] = {}
            for child_key, child_value in value.items():
                label = str(child_key)
                if permitted is None:
                    if not (re.fullmatch(r"\d{3}", label) or label == "none"):
                        continue
                elif label not in permitted:
                    continue
                if isinstance(child_value, (Mapping, list, tuple, set)):
                    continue
                child[label] = _safe_scalar(label, child_value)
            result[key] = child
        else:
            result[key] = _safe_scalar(key, value)
    return result


def _safe_environment(environment: Mapping[str, Any]) -> dict[str, Any]:
    params = environment.get("parameters")
    safe_params: dict[str, Any] = {}
    if isinstance(params, Mapping):
        for key in (*SCENARIO_KEYS, "duration"):
            if key in params:
                # ``duration`` is the runner's historical spelling; the
                # scenario map exposes its canonical duration key separately.
                safe_params[key] = _safe_scalar(key, params[key])
    target = environment.get("target")
    safe_target: dict[str, Any] = {}
    if isinstance(target, Mapping) and target.get("base_url") is not None:
        safe_target["base_url"] = redact_url(str(target.get("base_url")))
    # Version/host facts are useful for interpreting a comparison and do not
    # contain request content.  Keep only known scalar keys.
    safe: dict[str, Any] = {"parameters": safe_params}
    if safe_target:
        safe["target"] = safe_target
    for key in ("git_commit", "python", "python_implementation", "os", "machine", "cpu_count"):
        if key in environment:
            safe[key] = _safe_scalar(key, environment[key])
    return safe


def compare_rounds(
    direct: RoundArtifacts | str | Path | Mapping[str, Any],
    gateway: RoundArtifacts | str | Path | Mapping[str, Any],
    *,
    strict_scenario: bool = False,
    raise_on_incomparable: bool = False,
) -> dict[str, Any]:
    """Return a JSON-safe direct-to-gateway comparison.

    ``delta`` always means ``gateway - direct``.  Metrics remain visible when
    scenario metadata conflicts, but ``comparable`` is false and callers can
    request an exception with ``raise_on_incomparable``.  This prevents an
    accidental consumer from treating an incomparable merge as a valid result.
    """

    direct_round = _coerce_round(direct)
    gateway_round = _coerce_round(gateway)
    scenarios = compare_scenarios(direct_round, gateway_round, strict=strict_scenario)
    if raise_on_incomparable and not scenarios["comparable"]:
        details = "; ".join(item["parameter"] for item in scenarios["mismatches"])
        raise IncomparableRunsError(f"benchmark rounds are not comparable: {details}")
    metrics = _build_metrics(direct_round, gateway_round)
    return {
        "schema_version": 1,
        "comparable": scenarios["comparable"],
        "scenario_verified": scenarios["verified"],
        "scenario": scenarios,
        "direction": "gateway_minus_direct",
        "runs": {
            "direct": {
                # Paths may contain tenant names or local directory details;
                # comparison artifacts need only the stable role label.
                "source": "direct",
                "environment": _safe_environment(direct_round.environment),
                "summary": _safe_summary(direct_round.summary),
            },
            "gateway": {
                "source": "gateway",
                "environment": _safe_environment(gateway_round.environment),
                "summary": _safe_summary(gateway_round.summary),
            },
        },
        "metrics": metrics,
        # Top-level aliases are intentionally concise for automation.
        "latency": metrics["latency_ms"],
        "ttft": metrics["ttft_ms"],
        "throughput": metrics["throughput"],
        "rates": metrics["rates"],
        "usage": metrics["usage"],
    }


def compare_results(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for :func:`compare_rounds`."""

    return compare_rounds(*args, **kwargs)


def compare_summaries(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compare two summary mappings or summary paths."""

    return compare_rounds(*args, **kwargs)


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{value:.6g}" if isinstance(value, float) else str(value)
    return str(value)


def _md_label(value: Any) -> str:
    """Return a bounded inline label without Markdown delimiters."""

    return str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")


def render_markdown(comparison: Mapping[str, Any]) -> str:
    """Render a redacted human-readable comparison report."""

    comparable = bool(comparison.get("comparable"))
    verified = bool(comparison.get("scenario_verified"))
    lines = [
        "# Rotor direct-to-gateway benchmark comparison",
        "",
        f"- Comparable: **{'yes' if comparable else 'no'}**",
        f"- Scenario metadata verified: **{'yes' if verified else 'no'}**",
        "- Delta direction: `gateway - direct`",
        "",
        "## Scenario checks",
        "",
    ]
    scenario = comparison.get("scenario") if isinstance(comparison.get("scenario"), Mapping) else {}
    mismatches = scenario.get("mismatches", []) if isinstance(scenario, Mapping) else []
    checked = scenario.get("checked", {}) if isinstance(scenario, Mapping) else {}
    if isinstance(checked, Mapping) and checked:
        lines.extend(
            f"- `{_md_label(key)}`: `{_fmt(value.get('direct'))}` vs `{_fmt(value.get('gateway'))}`"
            for key, value in checked.items()
            if isinstance(value, Mapping)
        )
    else:
        lines.append("- No scenario controls were recorded; treat metrics as unverified.")
    if mismatches:
        lines.extend(
            f"- **Mismatch `{_md_label(item.get('parameter', 'unknown'))}`**: "
            f"{_md_label(item.get('reason', 'values differ'))} "
            f"(direct `{_fmt(item.get('direct'))}`, gateway `{_fmt(item.get('gateway'))}`)"
            for item in mismatches
            if isinstance(item, Mapping)
        )
    lines.extend(["", "## Metrics", "", "| Metric | Direct | Gateway | Delta | Relative delta |", "|---|---:|---:|---:|---:|"])
    metrics = comparison.get("metrics") if isinstance(comparison.get("metrics"), Mapping) else {}
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for group, label in (("latency_ms", "Latency ms"), ("ttft_ms", "TTFT ms"), ("throughput", "Throughput"), ("rates", "Rates")):
        values = metrics.get(group) if isinstance(metrics, Mapping) else None
        if isinstance(values, Mapping):
            for name, entry in values.items():
                if isinstance(entry, Mapping):
                    rows.append((f"{label} / {name}", entry))
    for label, entry in rows:
        lines.append(
            f"| {_safe_scalar('label', label)} | {_fmt(entry.get('direct'))} | "
            f"{_fmt(entry.get('gateway'))} | {_fmt(entry.get('delta'))} | "
            f"{_fmt(entry.get('relative_delta_percent'))}% |"
        )
    lines.extend(["", "## Usage", "", "| Counter | Direct | Gateway | Delta |", "|---|---:|---:|---:|"])
    usage = metrics.get("usage") if isinstance(metrics, Mapping) else {}
    if isinstance(usage, Mapping):
        for name, entry in usage.items():
            if isinstance(entry, Mapping):
                lines.append(
                    f"| {_safe_scalar('label', name)} | {_fmt(entry.get('direct'))} | "
                    f"{_fmt(entry.get('gateway'))} | {_fmt(entry.get('delta'))} |"
                )
    lines.extend([
        "",
        "Only whitelisted, redacted metadata is included. API keys, prompts, "
        "headers, response text, and raw request rows are intentionally omitted.",
        "",
    ])
    return "\n".join(lines)


def _json_text(comparison: Mapping[str, Any]) -> str:
    return json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare direct and gateway benchmark result rounds")
    parser.add_argument("direct_path", nargs="?", help="Direct-provider result directory or summary.json")
    parser.add_argument("gateway_path", nargs="?", help="Gateway result directory or summary.json")
    parser.add_argument("--direct", "--baseline", "--direct-dir", dest="direct_option", help="Direct round path")
    parser.add_argument("--gateway", "--candidate", "--gateway-dir", dest="gateway_option", help="Gateway round path")
    parser.add_argument("--format", choices=("json", "markdown", "md"), default="json")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output file (default: stdout)")
    parser.add_argument("--strict-scenario", action="store_true", help="Treat absent scenario metadata as incomparable")
    parser.add_argument("--allow-incomparable", action="store_true", help="Exit zero even when scenario values conflict")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    direct = args.direct_option or args.direct_path
    gateway = args.gateway_option or args.gateway_path
    if not direct or not gateway:
        parser.error("provide direct and gateway paths (positionally or with --direct/--gateway)")
    try:
        comparison = compare_rounds(direct, gateway, strict_scenario=args.strict_scenario)
        rendered = render_markdown(comparison) if args.format in {"markdown", "md"} else _json_text(comparison)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except CompareError as exc:
        print(f"compare error: {exc}", file=sys.stderr)
        return 2
    if not comparison.get("comparable") and not args.allow_incomparable:
        return 3
    return 0


__all__ = [
    "CompareError",
    "IncomparableRunsError",
    "RoundArtifacts",
    "SCENARIO_KEYS",
    "SCENARIO_REQUIRED_KEYS",
    "SCENARIO_WORKLOAD_KEYS",
    "build_parser",
    "compare_results",
    "compare_rounds",
    "compare_scenarios",
    "compare_summaries",
    "load_round",
    "main",
    "render_markdown",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
