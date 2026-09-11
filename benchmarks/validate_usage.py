"""Read-only SQLite usage snapshots and accounting comparison.

The validator never updates the database and never selects credential/body
columns.  It is intentionally independent of SQLAlchemy so it can inspect a
database while Rotor is running; SQLite's normal read-only URI mode also sees
WAL contents.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote, urlparse


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "request_logs": (
        "id", "token_id", "channel_id", "model", "request_model",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "uncached_input_tokens", "cached_tokens", "cache_write_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens", "success",
        "error_code", "latency", "created_at",
    ),
    "usage_ledger": (
        "id", "request_id", "conversation_id", "user_id", "token_id",
        "channel_id", "provider", "model", "provider_model",
        "request_protocol", "provider_protocol", "prompt_tokens",
        "completion_tokens", "total_tokens", "uncached_input_tokens",
        "cached_tokens", "cache_write_tokens", "cache_write_5m_tokens",
        "cache_write_1h_tokens", "reasoning_tokens", "status", "error_code",
        "latency_ms", "created_at",
    ),
    "request_attempts": (
        "id", "request_id", "attempt_index", "channel_id", "requested_model",
        "provider_model", "request_protocol", "started_at", "finished_at",
        "latency_ms", "outcome", "upstream_status", "error_category",
        "error_code", "retryable", "fallback_allowed",
    ),
    # ``key`` is deliberately absent.  Only counters and stable identity are
    # needed to validate quota deltas.
    "tokens": (
        "id", "name", "user_id", "quota", "used_quota", "group", "enabled",
        "expired", "request_count", "token_count", "created_at", "updated_at",
        "last_used_at",
    ),
    "channels": (
        "id", "name", "type", "priority", "weight", "enabled", "test_only",
        "protocol", "rpm_limit", "tpm_limit", "created_at", "updated_at",
    ),
}
NUMERIC_FIELDS = {
    "prompt_tokens", "completion_tokens", "total_tokens", "uncached_input_tokens",
    "cached_tokens", "cache_write_tokens", "cache_write_5m_tokens",
    "cache_write_1h_tokens", "reasoning_tokens", "used_quota", "request_count",
    "token_count",
}
# Minimum safe columns needed for a core accounting comparison.  Other
# columns are intentionally optional because Rotor schema revisions add them
# over time; a table containing none of these columns is not usable evidence.
REQUIRED_COLUMNS = {
    "request_logs": {"total_tokens"},
    "usage_ledger": {"total_tokens"},
    "tokens": {"used_quota"},
}


class UsageValidationError(RuntimeError):
    """A clear, user-facing snapshot/compare error."""


def _sqlite_path(value: str | Path) -> Path:
    text = str(value)
    if text.startswith(("postgres://", "postgresql://", "postgresql+", "postgres+", "mysql://", "mysql+")):
        raise UsageValidationError(
            "usage snapshot reader supports SQLite only; use an isolated SQL snapshot for non-SQLite databases"
        )
    if text.startswith("sqlite+") and ":///" in text:
        # Three slashes denotes a relative SQLAlchemy path; a fourth slash
        # leaves the leading slash and denotes an absolute path.
        text = text.split(":///", 1)[1]
    elif text.startswith("sqlite:///"):
        text = text[len("sqlite:///") :]
    elif text.startswith("sqlite://"):
        text = text[len("sqlite://") :]
    # SQLAlchemy URL query parameters (if any) are connection options, not
    # part of the filesystem path.  Never let them become a literal filename.
    if "?" in text and not text.startswith("file:"):
        text = text.split("?", 1)[0]
    if text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
    path = Path(text).expanduser()
    if not path.exists():
        raise UsageValidationError(f"SQLite database is not readable or does not exist: {path}")
    if not path.is_file():
        raise UsageValidationError(f"SQLite database path is not a file: {path}")
    return path


def _connect_readonly(value: str | Path) -> tuple[sqlite3.Connection, Path]:
    path = _sqlite_path(value)
    # mode=ro prevents accidental writes.  Do not use immutable=1: it would
    # hide a live database's WAL file and produce a false before/after delta.
    # Quote spaces/#/unicode while preserving path separators for SQLite URI.
    encoded_path = quote(str(path.resolve()), safe="/\\:")
    uri = f"file:{encoded_path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection, path
    except (sqlite3.Error, OSError) as exc:
        raise UsageValidationError(f"unable to open SQLite database read-only: {path}: {exc}") from exc


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        # SQLite's default CURRENT_TIMESTAMP has a space and no timezone.
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _row_in_window(
    row: Mapping[str, Any],
    since: float | None,
    until: float | None,
    *,
    table: str | None = None,
) -> bool:
    if since is None and until is None:
        return True
    if table in {"tokens", "channels"}:
        # These tables hold mutable/cumulative state.  Filtering rows by their
        # creation/update timestamp loses the before baseline (e.g. a token
        # created last month but quota incremented today), so retain all rows
        # and calculate counter deltas between snapshots.
        return True
    if table == "request_attempts":
        started = _parse_time(row.get("started_at"))
        finished = _parse_time(row.get("finished_at"))
        if started is not None and finished is not None:
            # A long attempt belongs to a window when its interval overlaps
            # it, even if it started before ``since``.
            return (until is None or started < until) and (since is None or finished >= since)
        if started is not None:
            # Missing finish is conservatively retained when it could still
            # be active at the window boundary.
            return until is None or started < until
    timestamp_fields = ("created_at", "started_at", "updated_at", "last_used_at")
    timestamps = [
        parsed
        for name in timestamp_fields
        if row.get(name) is not None
        for parsed in [_parse_time(row.get(name))]
        if parsed is not None
    ]
    # Rows with an unparsable timestamp are retained rather than silently
    # dropping possible usage; explicit request-id filters can narrow them.
    if not timestamps:
        return True
    # Mutable rows (tokens/channels) can have an old created_at and a new
    # updated_at; any timestamp in the requested interval should retain them.
    return any(
        (since is None or stamp >= since) and (until is None or stamp < until)
        for stamp in timestamps
    )


def _row_matches_ids(row: Mapping[str, Any], request_ids: set[str] | None, table: str) -> bool:
    if not request_ids:
        return True
    request_id = row.get("request_id")
    if request_id is not None:
        return str(request_id) in request_ids
    # RequestLog in the current Rotor schema also has no request_id (only an
    # auto-increment id).  Retain rows without that column and use aggregate or
    # time-window deltas; silently dropping them would make request-id scoped
    # snapshots look empty and falsely fail accounting checks.
    return True


def _table_snapshot(
    connection: sqlite3.Connection,
    table: str,
    *,
    request_ids: set[str] | None,
    since: float | None,
    until: float | None,
) -> dict[str, Any]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return {
            "exists": False,
            "usable": False,
            "selected_columns": [],
            "row_count": 0,
            "numeric_sums": {},
            "rows": [],
            "request_ids": [],
        }
    actual_columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    columns = [column for column in TABLE_COLUMNS[table] if column in actual_columns]
    if not columns:
        return {
            "exists": True,
            "usable": False,
            "selected_columns": [],
            "row_count": 0,
            "numeric_sums": {},
            "rows": [],
            "request_ids": [],
        }
    quoted = ", ".join(f'"{column}"' for column in columns)
    try:
        raw_rows = connection.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
    except sqlite3.Error as exc:
        raise UsageValidationError(f"unable to read table {table!r}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = {column: raw[column] for column in columns}
        if not _row_matches_ids(row, request_ids, table) or not _row_in_window(row, since, until, table=table):
            continue
        # Convert bytes to a safe printable marker; no body/blob values are
        # selected by the whitelist, but this keeps JSON serialization robust.
        for key, value in list(row.items()):
            if isinstance(value, bytes):
                row[key] = f"<bytes:{len(value)}>"
        rows.append(row)
    sums: dict[str, int] = {}
    for row in rows:
        for field in NUMERIC_FIELDS.intersection(row):
            try:
                sums[field] = sums.get(field, 0) + int(row[field] or 0)
            except (TypeError, ValueError):
                continue
    ids = sorted({str(row["request_id"]) for row in rows if row.get("request_id") is not None})
    required = REQUIRED_COLUMNS.get(table, set())
    return {
        "exists": True,
        "usable": required.issubset(columns),
        "selected_columns": columns,
        "row_count": len(rows),
        "numeric_sums": sums,
        "rows": rows,
        "request_ids": ids,
    }


def snapshot_database(
    database: str | Path,
    *,
    request_ids: Iterable[str] | None = None,
    since: float | str | None = None,
    until: float | str | None = None,
) -> dict[str, Any]:
    """Take a safe, read-only snapshot of the accounting tables."""

    ids = {str(value) for value in request_ids or () if str(value)}
    since_value = _parse_time(since)
    until_value = _parse_time(until)
    if since is not None and since_value is None:
        raise UsageValidationError(f"invalid --since timestamp: {since}")
    if until is not None and until_value is None:
        raise UsageValidationError(f"invalid --until timestamp: {until}")
    if since_value is not None and until_value is not None and since_value > until_value:
        raise UsageValidationError("since timestamp must not be later than until timestamp")
    # Validate filters before opening a connection so malformed CLI input does
    # not leave a read-only handle undisposed.
    connection, path = _connect_readonly(database)
    captured = time.time()
    try:
        # Pin one consistent read view across all five tables.  A deferred
        # transaction is still read-only and includes committed WAL frames.
        connection.execute("BEGIN DEFERRED")
        tables = {
            table: _table_snapshot(
                connection,
                table,
                request_ids=ids or None,
                since=since_value,
                until=until_value,
            )
            for table in TABLE_COLUMNS
        }
    finally:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        connection.close()
    return {
        "captured_at": captured,
        "database": {"type": "sqlite", "path": str(path.resolve())},
        "filters": {
            "request_ids": sorted(ids),
            "since": since_value,
            "until": until_value,
        },
        "tables": tables,
    }


def _row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    if row.get("id") is not None:
        return ("id", row.get("id"))
    return tuple(sorted((str(k), str(v)) for k, v in row.items()))


def _numeric_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
    fields = set(before) | set(after)
    return {
        field: int(after.get(field, 0) or 0) - int(before.get(field, 0) or 0)
        for field in sorted(fields)
        if field in NUMERIC_FIELDS
    }


def _table_available(table: str, value: Any) -> bool:
    """Return whether a snapshot contains usable evidence for ``table``."""

    if not isinstance(value, Mapping) or not value.get("exists", False):
        return False
    if "usable" in value:
        return bool(value.get("usable"))
    # Snapshots written by an older MVP version did not include schema
    # metadata.  Treat those as usable for backwards compatibility; newly
    # captured snapshots always carry the explicit flag above.
    selected = value.get("selected_columns")
    if selected is not None:
        required = REQUIRED_COLUMNS.get(table, set())
        return required.issubset(set(selected))
    return True


def _filter_table_snapshot(
    table: str,
    snapshot: Mapping[str, Any],
    requested: set[str],
) -> dict[str, Any]:
    """Apply an explicit request-id filter even to broad saved snapshots."""

    if not requested:
        return dict(snapshot)
    rows = [
        row for row in snapshot.get("rows", [])
        if isinstance(row, Mapping)
        and (row.get("request_id") is None or str(row.get("request_id")) in requested)
    ]
    sums: dict[str, int] = {}
    for row in rows:
        for field in NUMERIC_FIELDS.intersection(row):
            try:
                sums[field] = sums.get(field, 0) + int(row.get(field) or 0)
            except (TypeError, ValueError):
                continue
    ids = sorted({str(row["request_id"]) for row in rows if row.get("request_id") is not None})
    filtered = dict(snapshot)
    filtered.update({"row_count": len(rows), "numeric_sums": sums, "rows": rows, "request_ids": ids})
    return filtered


def compare_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    request_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Compare snapshots and flag token/accounting mismatches."""

    requested = {str(value) for value in request_ids or () if str(value)}
    before_tables = before.get("tables", {}) if isinstance(before, Mapping) else {}
    after_tables = after.get("tables", {}) if isinstance(after, Mapping) else {}
    tables: dict[str, Any] = {}
    all_request_ids: set[str] = set(requested)
    for table in TABLE_COLUMNS:
        old = before_tables.get(table, {}) if isinstance(before_tables, Mapping) else {}
        new = after_tables.get(table, {}) if isinstance(after_tables, Mapping) else {}
        old = _filter_table_snapshot(table, old, requested) if isinstance(old, Mapping) else {}
        new = _filter_table_snapshot(table, new, requested) if isinstance(new, Mapping) else {}
        old_rows = {_row_key(row): row for row in old.get("rows", []) if isinstance(row, Mapping)}
        new_rows = {_row_key(row): row for row in new.get("rows", []) if isinstance(row, Mapping)}
        changed = []
        for key, row in new_rows.items():
            if key not in old_rows or old_rows[key] != row:
                changed.append(row)
        old_sums = old.get("numeric_sums", {}) or {}
        new_sums = new.get("numeric_sums", {}) or {}
        sums = _numeric_delta(old_sums, new_sums)
        ids = set(old.get("request_ids", []) or ()) | set(new.get("request_ids", []) or ())
        all_request_ids.update(str(value) for value in ids)
        tables[table] = {
            "before_exists": bool(old.get("exists", False)),
            "after_exists": bool(new.get("exists", False)),
            "before_usable": _table_available(table, old),
            "after_usable": _table_available(table, new),
            "before_columns": list(old.get("selected_columns", []) or []),
            "after_columns": list(new.get("selected_columns", []) or []),
            "before_rows": int(old.get("row_count", 0) or 0),
            "after_rows": int(new.get("row_count", 0) or 0),
            "row_delta": int(new.get("row_count", 0) or 0) - int(old.get("row_count", 0) or 0),
            "numeric_delta": sums,
            "changed_rows": len(changed),
            "request_ids": sorted(str(value) for value in ids),
        }

    mismatches: list[dict[str, Any]] = []
    before_database = before.get("database") if isinstance(before, Mapping) else None
    after_database = after.get("database") if isinstance(after, Mapping) else None
    before_path = before_database.get("path") if isinstance(before_database, Mapping) else None
    after_path = after_database.get("path") if isinstance(after_database, Mapping) else None
    if before_path and after_path:
        try:
            same_database = Path(str(before_path)).resolve() == Path(str(after_path)).resolve()
        except (OSError, RuntimeError):
            same_database = str(before_path) == str(after_path)
        if not same_database:
            mismatches.append({
                "kind": "database_identity",
                "before": str(before_path),
                "after": str(after_path),
                "message": "before and after snapshots must reference the same database",
            })
    # Comparing snapshots captured with different filters is ambiguous.  An
    # explicit request_ids argument intentionally overrides this check (and
    # filters both saved snapshots below); otherwise surface a clear mismatch
    # instead of silently producing a negative/partial delta.
    if not requested:
        before_filter = before.get("filters") if isinstance(before, Mapping) else None
        after_filter = after.get("filters") if isinstance(after, Mapping) else None
        if isinstance(before_filter, Mapping) and isinstance(after_filter, Mapping):
            normalized_before = {
                "request_ids": sorted(str(v) for v in before_filter.get("request_ids", []) or []),
                "since": before_filter.get("since"),
                "until": before_filter.get("until"),
            }
            normalized_after = {
                "request_ids": sorted(str(v) for v in after_filter.get("request_ids", []) or []),
                "since": after_filter.get("since"),
                "until": after_filter.get("until"),
            }
            if normalized_before != normalized_after:
                mismatches.append({
                    "kind": "snapshot_filters",
                    "before": normalized_before,
                    "after": normalized_after,
                    "message": "capture before and after with identical filters, or pass request_ids explicitly",
                })
    logs_total = tables["request_logs"]["numeric_delta"].get("total_tokens", 0)
    ledger_total = tables["usage_ledger"]["numeric_delta"].get("total_tokens", 0)
    # If one side has no table/rows, do not report a false mismatch; missing
    # accounting evidence is reported separately and remains visible.
    logs_exists = _table_available("request_logs", after_tables.get("request_logs"))
    ledger_exists = _table_available("usage_ledger", after_tables.get("usage_ledger"))
    tokens_exists = _table_available("tokens", after_tables.get("tokens"))
    checks_skipped: list[str] = []
    logs_are_request_scoped = bool(tables["request_logs"].get("request_ids"))
    if logs_exists and ledger_exists and requested and not logs_are_request_scoped:
        # Current RequestLog rows have no request_id.  Comparing their
        # aggregate delta to a request-id-filtered ledger would mix unrelated
        # traffic and create a false mismatch, so leave the aggregate check
        # out while retaining the ledger/quota check below.
        checks_skipped.append("request_logs_vs_usage_ledger_token_equality")
    elif logs_exists and ledger_exists:
        log_delta = tables["request_logs"]["numeric_delta"]
        ledger_delta = tables["usage_ledger"]["numeric_delta"]
        for field, kind in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "token_total"),
        ):
            log_value = log_delta.get(field, 0)
            ledger_value = ledger_delta.get(field, 0)
            if (log_value or ledger_value) and log_value != ledger_value:
                mismatches.append({"kind": kind, "request_logs": log_value, "usage_ledger": ledger_value})

    quota_delta = tables["tokens"]["numeric_delta"].get("used_quota", 0)
    if tokens_exists and ledger_exists:
        if requested:
            # ``tokens.used_quota`` is a global cumulative counter and cannot
            # be attributed to one request_id without a token-level ledger.
            # Comparing it with a filtered ledger would mix unrelated traffic.
            checks_skipped.append("tokens_vs_usage_ledger_used_quota")
        elif quota_delta != ledger_total and (quota_delta or ledger_total):
            mismatches.append({"kind": "used_quota", "tokens": quota_delta, "usage_ledger": ledger_total})

    # RequestLog has no request_id column in Rotor's schema, so a per-request
    # ledger→log join is not possible.  Keep this limitation explicit rather
    # than reporting every ledger row as an orphan; totals and time windows
    # remain valid checks.
    notes = [
        "request_logs has no request_id column; request-id filters use ledger/attempt IDs and aggregate log deltas"
    ]
    if checks_skipped:
        notes.append(
            "some request-id scoped equality checks were skipped because the corresponding counters are not request-scoped: "
            + ", ".join(checks_skipped)
        )
    ledger_before = before_tables.get("usage_ledger", {}) if isinstance(before_tables, Mapping) else {}
    ledger_after = after_tables.get("usage_ledger", {}) if isinstance(after_tables, Mapping) else {}
    attempts_before = before_tables.get("request_attempts", {}) if isinstance(before_tables, Mapping) else {}
    attempts_after = after_tables.get("request_attempts", {}) if isinstance(after_tables, Mapping) else {}
    if requested:
        ledger_before = _filter_table_snapshot("usage_ledger", ledger_before, requested)
        ledger_after = _filter_table_snapshot("usage_ledger", ledger_after, requested)
        attempts_before = _filter_table_snapshot("request_attempts", attempts_before, requested)
        attempts_after = _filter_table_snapshot("request_attempts", attempts_after, requested)
    ledger_before_ids = {
        str(row.get("request_id")) for row in ledger_before.get("rows", [])
        if isinstance(row, Mapping) and row.get("request_id") is not None
    }
    ledger_after_ids = {
        str(row.get("request_id")) for row in ledger_after.get("rows", [])
        if isinstance(row, Mapping) and row.get("request_id") is not None
    }
    attempt_before_ids = {
        str(row.get("request_id")) for row in attempts_before.get("rows", [])
        if isinstance(row, Mapping) and row.get("request_id") is not None
    }
    attempt_after_ids = {
        str(row.get("request_id")) for row in attempts_after.get("rows", [])
        if isinstance(row, Mapping) and row.get("request_id") is not None
    }
    ledger_exists_after = _table_available("usage_ledger", ledger_after)
    attempts_exists_after = _table_available("request_attempts", attempts_after)
    ledger_before_rows = [
        row for row in ledger_before.get("rows", []) if isinstance(row, Mapping)
    ]
    ledger_after_rows = [
        row for row in ledger_after.get("rows", []) if isinstance(row, Mapping)
    ]
    before_ledger_by_key = {_row_key(row): row for row in ledger_before_rows}
    changed_ledger_rows = [
        row for key, row in {_row_key(row): row for row in ledger_after_rows}.items()
        if key not in before_ledger_by_key or before_ledger_by_key[key] != row
    ]
    # Exactly one usage-ledger row is expected per request.  For a scoped
    # comparison inspect all rows in scope; for a broad comparison inspect the
    # newly inserted/changed rows so historical anomalies do not repeat forever.
    duplicate_candidates = ledger_after_rows if requested else changed_ledger_rows
    candidate_ids = {
        str(row.get("request_id")) for row in duplicate_candidates
        if row.get("request_id") is not None
    }
    after_id_counts = Counter(
        str(row.get("request_id")) for row in ledger_after_rows
        if row.get("request_id") is not None and str(row.get("request_id")) in candidate_ids
    )
    duplicate_request_ids = {
        request_id: count for request_id, count in sorted(after_id_counts.items()) if count > 1
    }
    if duplicate_request_ids:
        mismatches.append({
            "kind": "duplicate_request_ids",
            "request_ids": sorted(duplicate_request_ids),
            "counts": duplicate_request_ids,
        })
    if ledger_exists_after and attempts_exists_after:
        missing_attempts = sorted((ledger_after_ids - ledger_before_ids) - (attempt_after_ids - attempt_before_ids))
        if missing_attempts:
            mismatches.append({"kind": "missing_request_attempts", "request_ids": missing_attempts})
    required_tables = ("request_logs", "usage_ledger", "request_attempts", "tokens", "channels")
    evidence_missing: list[str] = []
    for required_table in required_tables:
        before_data = before_tables.get(required_table) if isinstance(before_tables, Mapping) else None
        after_data = after_tables.get(required_table) if isinstance(after_tables, Mapping) else None
        if not _table_available(required_table, before_data):
            evidence_missing.append(f"before.{required_table}")
        if not _table_available(required_table, after_data):
            evidence_missing.append(f"after.{required_table}")
    # The three core accounting tables are needed to claim a token agreement.
    # Attempts/channels are useful supplementary evidence but may be absent in
    # older Rotor schemas; report them without turning an otherwise valid core
    # delta into a false failure.
    core_tables = {"request_logs", "usage_ledger", "tokens"}
    core_evidence_missing = [
        item for item in evidence_missing
        if item.split(".", 1)[-1] in core_tables
    ]
    checks_complete = not core_evidence_missing
    if evidence_missing:
        notes.append(
            "snapshot table evidence is missing: "
            + ", ".join(evidence_missing)
        )
    if core_evidence_missing:
        notes.append("core accounting checks are incomplete because request_logs, usage_ledger, and tokens must exist in both snapshots")

    return {
        # A missing table must never look like a successful zero delta.  Keep
        # the explicit flag as well as making the CLI exit non-zero so callers
        # cannot accidentally treat an incomplete snapshot as validated.
        "ok": not mismatches and checks_complete,
        "checks_complete": checks_complete,
        "evidence_missing": evidence_missing,
        "before_captured_at": before.get("captured_at"),
        "after_captured_at": after.get("captured_at"),
        "request_ids": sorted(all_request_ids),
        "tables": tables,
        "mismatches": mismatches,
        "duplicate_request_ids": duplicate_request_ids,
        "checks_skipped": checks_skipped,
        "notes": notes,
    }


def validate_usage(
    before: Mapping[str, Any], after: Mapping[str, Any], *, request_ids: Iterable[str] | None = None
) -> dict[str, Any]:
    """Alias with a descriptive name for programmatic callers."""

    return compare_snapshots(before, after, request_ids=request_ids)


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageValidationError(f"unable to read snapshot {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("tables"), Mapping):
        raise UsageValidationError(f"snapshot {path} has an invalid format")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only SQLite before/after usage snapshot and mismatch validator"
    )
    parser.add_argument("database", nargs="?", help="SQLite path or sqlite:/// URL")
    parser.add_argument("--db", dest="database_option", help="SQLite path (alternative to positional)")
    parser.add_argument("--before", help="Existing before snapshot JSON")
    parser.add_argument("--after", help="Existing after snapshot JSON")
    parser.add_argument("--snapshot-out", help="Write a newly captured snapshot here")
    parser.add_argument("--request-id", action="append", dest="request_ids", default=[])
    parser.add_argument("--since", help="UTC epoch or ISO timestamp filter")
    parser.add_argument("--until", help="UTC epoch or ISO timestamp filter")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.database_option or args.database
    try:
        if args.before and args.after:
            result = compare_snapshots(
                _load_json(args.before),
                _load_json(args.after),
                request_ids=args.request_ids,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        if not database:
            raise UsageValidationError("provide a database path with --db or positional argument")
        snapshot = snapshot_database(
            database,
            request_ids=args.request_ids,
            since=args.since,
            until=args.until,
        )
        rendered = json.dumps(snapshot, ensure_ascii=False, indent=2)
        if args.snapshot_out:
            Path(args.snapshot_out).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return 0
    except UsageValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: unable to write snapshot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "TABLE_COLUMNS",
    "UsageValidationError",
    "compare_snapshots",
    "snapshot_database",
    "validate_usage",
    "build_parser",
    "main",
]
