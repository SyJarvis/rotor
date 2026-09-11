"""Artifact and environment reporting for benchmark rounds."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .metrics import RequestResult, summarize_results


def redact_api_key(value: str | None) -> str | None:
    """Return a non-reversible presence/fingerprint marker, never the key."""

    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"sha256:{digest}"


def redact_url(value: str | None) -> str | None:
    """Remove URL credentials and query/fragment values while retaining target.

    Non-HTTP DSNs (notably SQLite URLs) are sanitized too.  If a value cannot
    be parsed safely, a constant marker is returned instead of echoing a
    possibly credential-bearing string.
    """

    if not value:
        return value
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme and parsed.netloc:
            # urlsplit().hostname/port avoid preserving userinfo.  A malformed
            # port should still result in a safe generic URL.
            host = parsed.hostname or ""
            try:
                port = parsed.port
            except ValueError:
                port = None
            netloc = host
            if ":" in host and not host.startswith("["):
                netloc = f"[{host}]"
            if port is not None:
                netloc += f":{port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        if parsed.scheme:
            # DSN/path style values have no host (e.g. sqlite:///tmp/db).  We
            # can safely retain the path while dropping query/fragment data.
            return f"{parsed.scheme}://{parsed.path}"
    except Exception:
        return "<redacted-url>"
    # A non-URL value is not expected.  Plain filesystem paths are useful for
    # local diagnostics; strings containing query/userinfo markers are not.
    text = str(value)
    if any(marker in text for marker in ("?", "&", "@")):
        return "<redacted-url>"
    # Keep recognizable local paths, but do not echo an opaque bare token
    # supplied where a URL/DSN was expected.
    if text.startswith(("/", "./", "../", "~")) or "/" in text or text.endswith((".db", ".sqlite", ".sqlite3")):
        return text
    return "<redacted-url>"


def database_type(value: str | None) -> str:
    """Classify a database URL/path without opening it."""

    if not value:
        return "unknown"
    text = str(value).lower()
    if text.startswith("sqlite") or text.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite"
    if text.startswith(("postgres", "postgresql")):
        return "postgresql"
    if text.startswith("mysql"):
        return "mysql"
    return text.split(":", 1)[0] if ":" in text else "unknown"


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = completed.stdout.strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def _package_versions() -> dict[str, str | None]:
    names = {
        "rotor": "rotor-gateway",
        "httpx": "httpx",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "openai": "openai",
        "anthropic": "anthropic",
    }
    result: dict[str, str | None] = {}
    for label, distribution in names.items():
        try:
            result[label] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            # Rotor may be imported from src without an installed wheel.
            if label == "rotor":
                try:
                    import rotor

                    result[label] = str(getattr(rotor, "__version__", "unknown"))
                except Exception:
                    result[label] = None
            else:
                result[label] = None
    return result


def _safe_parameters(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy scenario parameters while masking key-like values recursively."""

    if not parameters:
        return {}
    secret_names = {
        "api_key", "apikey", "key", "token", "secret", "password",
        "authorization", "access_token", "auth_token", "bearer_token",
        "client_secret", "client_id_secret", "credential", "credentials",
    }

    def clean(key: str, value: Any) -> Any:
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        compact_secret_names = {
            "apikey", "accesstoken", "authtoken", "bearertoken", "clientsecret",
            "databaseurl", "connectionstring", "dsnpassword",
        }
        if (
            normalized in secret_names
            or normalized in compact_secret_names
            or normalized.endswith(("_key", "_token", "_secret"))
        ):
            if value:
                return redact_api_key(str(value))
            return None
        if (
            normalized.endswith(("_url", "url"))
            or normalized in {"url", "dsn", "database"}
            or (isinstance(value, str) and "://" in value)
        ):
            return redact_url(str(value)) if value is not None else None
        if isinstance(value, Mapping):
            return {str(k): clean(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(key, item) for item in value]
        if isinstance(value, Path):
            return str(value)
        return value

    return {str(key): clean(str(key), value) for key, value in parameters.items()}


def build_environment(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    database_url: str | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Collect reproducibility metadata with sensitive values removed."""

    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    db_value = database_url or os.getenv("DATABASE_URL")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(root_path),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "packages": _package_versions(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "database": {
            "type": database_type(db_value),
            "url": redact_url(db_value),
        },
        "target": {
            "base_url": redact_url(base_url),
            "api_key": redact_api_key(api_key),
            "api_key_present": bool(api_key),
        },
        "parameters": _safe_parameters(parameters),
    }


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def render_markdown(
    environment: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    protocol: str | None = None,
    stream: bool | None = None,
    concurrency: int | None = None,
    requests: int | None = None,
    rate: float | None = None,
) -> str:
    """Render a concise human-readable report with explicit measurement units."""

    params = environment.get("parameters") if isinstance(environment, Mapping) else {}
    target = environment.get("target", {}) if isinstance(environment, Mapping) else {}
    p = protocol or (params.get("protocol") if isinstance(params, Mapping) else None) or "unknown"
    s = stream if stream is not None else (params.get("stream") if isinstance(params, Mapping) else None)
    c = concurrency if concurrency is not None else (params.get("concurrency") if isinstance(params, Mapping) else None)
    n = requests if requests is not None else (params.get("requests") if isinstance(params, Mapping) else None)
    target_url = target.get("base_url") if isinstance(target, Mapping) else None
    latency = summary.get("latency_ms", {})
    ttft = summary.get("ttft_ms", {})
    errors = summary.get("errors", {})
    safe_target = _md_inline(target_url or "unknown")
    safe_protocol = _md_inline(p)
    lines = [
        "# Rotor benchmark report",
        "",
        f"- Target URL: `{safe_target}`",
        f"- Protocol: `{safe_protocol}`",
        f"- Streaming: `{bool(s)}`",
        f"- Concurrency: `{c if c is not None else 'unknown'}` (closed-loop workers)",
        f"- Requests: `{n if n is not None else summary.get('total_requests', 0)}` (warmup excluded)",
        f"- Rate: `{rate if rate is not None else 'not set'}` requests/s (optional launch cap)",
        "",
        "## Results",
        "",
        f"- Success: **{summary.get('successful_requests', 0)}** / {summary.get('total_requests', 0)} ({float(summary.get('success_rate', 0.0)) * 100:.2f}%)",
        f"- Failed: **{summary.get('failed_requests', 0)}**",
        f"- HTTP status codes: `{summary.get('status_codes', {})}`",
        f"- Wall-clock RPS: `{_fmt(summary.get('rps'))}` (measured window only)",
        f"- Successful RPS: `{_fmt(summary.get('successful_rps'))}`",
        f"- Latency ms (P50/P90/P95/P99/max): `{_fmt(latency.get('p50'))}` / `{_fmt(latency.get('p90'))}` / `{_fmt(latency.get('p95'))}` / `{_fmt(latency.get('p99'))}` / `{_fmt(latency.get('max'))}`",
        f"- TTFT ms (P50/P90/P95/P99/max): `{_fmt(ttft.get('p50'))}` / `{_fmt(ttft.get('p90'))}` / `{_fmt(ttft.get('p95'))}` / `{_fmt(ttft.get('p99'))}` / `{_fmt(ttft.get('max'))}`",
        f"- Output tokens/s (P50/P95/max): `{_fmt(summary.get('output_tokens_per_s', {}).get('p50'))}` / `{_fmt(summary.get('output_tokens_per_s', {}).get('p95'))}` / `{_fmt(summary.get('output_tokens_per_s', {}).get('max'))}`",
        f"- Stream chunks: `{_fmt(summary.get('chunk_count', {}).get('p50'))}` P50, `{_fmt(summary.get('chunk_count', {}).get('max'))}` max",
        "",
        "## Error categories",
        "",
    ]
    if errors:
        lines.extend(f"- `{_md_inline(name)}`: {count}" for name, count in sorted(errors.items()))
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "Warmup requests are intentionally absent from raw results and the RPS denominator. "
            "Streams count as successful only after the protocol terminal event is consumed. "
            "The per-request file contains IDs and metrics, never response text or credentials.",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


def _md_inline(value: Any) -> str:
    """Keep untrusted environment values from breaking Markdown fencing."""

    text = str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())


def safe_output_dir(path: str | Path, *, overwrite: bool = False) -> Path:
    """Create a result directory without silently replacing prior evidence."""

    requested = Path(path)
    if overwrite:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    if not requested.exists():
        try:
            # Atomic reservation avoids two concurrent rounds selecting the
            # same timestamp directory.
            requested.mkdir(parents=True, exist_ok=False)
            return requested
        except FileExistsError:
            pass
    index = 1
    while True:
        candidate = requested.parent / f"{requested.name}-{index}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            index += 1


def write_artifacts(
    output_dir: str | Path,
    *,
    results: Sequence[RequestResult | Mapping[str, Any]],
    summary: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any],
    report_text: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Write the four standard round artifacts and return their directory."""

    directory = safe_output_dir(output_dir, overwrite=overwrite)
    # Normalize mapping inputs through RequestResult as well.  Otherwise a
    # caller could bypass ``RequestResult.to_dict`` and persist a raw
    # conversation ID (or arbitrary request/body fields) in JSONL.
    rows = [
        row.to_dict() if isinstance(row, RequestResult) else RequestResult.from_mapping(row).to_dict()
        for row in results
    ]
    calculated = dict(summary or summarize_results(rows))
    (directory / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    with (directory / "raw-results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n")
    (directory / "summary.json").write_text(
        json.dumps(calculated, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    if report_text is None:
        report_text = render_markdown(environment, calculated)
    (directory / "report.md").write_text(report_text, encoding="utf-8")
    return directory


__all__ = [
    "build_environment",
    "database_type",
    "redact_api_key",
    "redact_url",
    "render_markdown",
    "safe_output_dir",
    "write_artifacts",
]
