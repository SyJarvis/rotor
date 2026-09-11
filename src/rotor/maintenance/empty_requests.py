"""Safely inspect and archive the known empty-stream request batch.

The predicate in this module is intentionally narrow.  A missing provider
usage block is not, by itself, evidence that a request is corrupt: some
providers legitimately omit usage.  The incident being repaired is the
successful OpenAI chat batch routed to channel 7 for ``gpt-5.6-sol`` where
every usage counter is zero.

The default operation is read-only.  Applying the cleanup requires both
``apply=True`` and an explicit ``confirm=True`` and archives the rows before
deleting them from the live database.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


EMPTY_REQUEST_PREDICATE = """
    u.status = 'success'
    AND u.usage_source = 'missing'
    AND u.prompt_tokens = 0
    AND u.completion_tokens = 0
    AND u.total_tokens = 0
    AND u.uncached_input_tokens = 0
    AND u.cached_tokens = 0
    AND u.cache_write_tokens = 0
    AND u.cache_write_5m_tokens = 0
    AND u.cache_write_1h_tokens = 0
    AND u.model = 'gpt-5.6-sol'
    AND u.channel_id = 7
    AND u.request_protocol = 'openai_chat'
"""

_REQUIRED_COLUMNS = {
    "usage_ledger": {
        "id",
        "request_id",
        "token_id",
        "channel_id",
        "model",
        "request_protocol",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "uncached_input_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "usage_source",
        "status",
    },
    "request_logs": {
        "id",
        "token_id",
        "channel_id",
        "model",
        "request_model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "uncached_input_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "success",
    },
}

_MAPPING_FIELDS = (
    "token_id",
    "channel_id",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "uncached_input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)

_RELATED_TABLES = (
    "request_attempts",
    "routing_decisions",
    "conversation_records",
)


class EmptyRequestCleanupError(RuntimeError):
    """Raised when cleanup cannot prove that the selected rows are safe."""


@dataclass(frozen=True)
class CleanupReport:
    """Counts produced by an inspection or cleanup run."""

    matched_usage_ledger: int
    matched_request_logs: int
    related_rows: dict[str, int] = field(default_factory=dict)
    archived_path: Path | None = None
    deleted_rows: dict[str, int] = field(default_factory=dict)

    @property
    def matched(self) -> int:
        return self.matched_usage_ledger


def inspect_empty_requests(database_path: str | Path) -> CleanupReport:
    """Return a read-only report for the incident-specific predicate."""

    path = _database_path(database_path)
    with _connect(path, read_only=True) as connection:
        _validate_schema(connection)
        _create_candidate_table(connection)
        candidates = _load_candidates(connection)
        _validate_request_log_mapping(connection, candidates)
        related_rows = _related_counts(connection)
    return CleanupReport(
        matched_usage_ledger=len(candidates),
        matched_request_logs=len(candidates),
        related_rows=related_rows,
    )


def cleanup_empty_requests(
    database_path: str | Path,
    *,
    archive_path: str | Path | None = None,
    apply: bool = False,
    confirm: bool = False,
) -> CleanupReport:
    """Inspect or archive/delete the known empty request batch.

    ``apply`` is deliberately false by default.  A write requires the caller
    to pass both ``apply=True`` and ``confirm=True``; the latter is intended to
    make an operator acknowledge that the live database will be changed.
    """

    if not apply:
        return inspect_empty_requests(database_path)
    if not confirm:
        raise EmptyRequestCleanupError(
            "refusing to write without explicit confirmation (confirm=True)"
        )

    source_path = _database_path(database_path)
    destination_path = (
        _default_archive_path(source_path)
        if archive_path is None
        else Path(archive_path).expanduser()
    )
    _check_archive_target(source_path, destination_path)

    connection = _connect(source_path, read_only=False)
    archive_created = False
    archive_committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_schema(connection)
        _create_candidate_table(connection)
        candidates = _load_candidates(connection)
        _validate_request_log_mapping(connection, candidates)
        related_rows = _related_counts(connection)
        if not candidates:
            connection.rollback()
            return CleanupReport(
                matched_usage_ledger=0,
                matched_request_logs=0,
                related_rows=related_rows,
            )

        _reserve_archive_path(destination_path)
        archive_created = True
        try:
            _write_archive(
                connection,
                destination_path,
                candidates,
                related_rows,
                source_path,
            )
            archive_committed = True
        except Exception:
            # An incomplete archive must never be mistaken for a recoverable
            # snapshot.  A fully committed archive is retained on later
            # source-transaction failure for forensic recovery.
            if archive_created and not archive_committed:
                _remove_reserved_archive(destination_path)
            raise

        deleted_rows = _delete_selected_rows(connection)
        _validate_delete_counts(deleted_rows, related_rows, len(candidates))
        connection.commit()
        return CleanupReport(
            matched_usage_ledger=len(candidates),
            matched_request_logs=len(candidates),
            related_rows=related_rows,
            archived_path=destination_path,
            deleted_rows=deleted_rows,
        )
    except EmptyRequestCleanupError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise EmptyRequestCleanupError(str(exc)) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _database_path(database_path: str | Path) -> Path:
    path = Path(database_path).expanduser()
    if not path.exists():
        raise EmptyRequestCleanupError(f"database does not exist: {path}")
    if not path.is_file():
        raise EmptyRequestCleanupError(f"database is not a file: {path}")
    return path.resolve()


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    try:
        if read_only:
            connection = sqlite3.connect(
                f"file:{path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        else:
            connection = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        raise EmptyRequestCleanupError(
            f"cannot open database {path}: {exc}"
        ) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return [str(row[1]) for row in rows]


def _validate_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        columns = set(_table_columns(connection, table))
        if not columns:
            raise EmptyRequestCleanupError(f"required table is missing: {table}")
        missing = sorted(required - columns)
        if missing:
            raise EmptyRequestCleanupError(
                f"{table} is missing required columns: {', '.join(missing)}"
            )


def _create_candidate_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _rotor_empty_candidates ("
        "ledger_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL)"
    )
    connection.execute("DELETE FROM _rotor_empty_candidates")
    connection.execute(
        "INSERT INTO _rotor_empty_candidates (ledger_id, request_id) "
        f"SELECT u.id, u.request_id FROM usage_ledger AS u WHERE {EMPTY_REQUEST_PREDICATE} "
        "ORDER BY u.id"
    )


def _load_candidates(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute(
        "SELECT ledger_id, request_id FROM _rotor_empty_candidates ORDER BY ledger_id"
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def _validate_request_log_mapping(
    connection: sqlite3.Connection,
    candidates: list[tuple[int, str]],
) -> None:
    if not candidates:
        return
    rows = connection.execute(
        """
        SELECT
            u.id AS usage_id,
            u.request_id,
            r.id AS log_id,
            u.token_id AS usage_token_id,
            r.token_id AS log_token_id,
            u.channel_id AS usage_channel_id,
            r.channel_id AS log_channel_id,
            u.model AS usage_model,
            r.model AS log_model,
            r.request_model AS log_request_model,
            u.prompt_tokens AS usage_prompt_tokens,
            r.prompt_tokens AS log_prompt_tokens,
            u.completion_tokens AS usage_completion_tokens,
            r.completion_tokens AS log_completion_tokens,
            u.total_tokens AS usage_total_tokens,
            r.total_tokens AS log_total_tokens,
            u.uncached_input_tokens AS usage_uncached_input_tokens,
            r.uncached_input_tokens AS log_uncached_input_tokens,
            u.cached_tokens AS usage_cached_tokens,
            r.cached_tokens AS log_cached_tokens,
            u.cache_write_tokens AS usage_cache_write_tokens,
            r.cache_write_tokens AS log_cache_write_tokens,
            u.cache_write_5m_tokens AS usage_cache_write_5m_tokens,
            r.cache_write_5m_tokens AS log_cache_write_5m_tokens,
            u.cache_write_1h_tokens AS usage_cache_write_1h_tokens,
            r.cache_write_1h_tokens AS log_cache_write_1h_tokens,
            r.success AS log_success
        FROM _rotor_empty_candidates AS c
        JOIN usage_ledger AS u ON u.id = c.ledger_id
        LEFT JOIN request_logs AS r ON r.id = u.id
        """
    ).fetchall()
    if len(rows) != len(candidates):
        raise EmptyRequestCleanupError(
            "request_logs mapping count mismatch; refusing to delete"
        )

    expected_ids = {ledger_id for ledger_id, _request_id in candidates}
    actual_ids = {int(row["usage_id"]) for row in rows}
    if actual_ids != expected_ids or any(row["log_id"] is None for row in rows):
        raise EmptyRequestCleanupError(
            "request_logs mapping is incomplete; refusing to delete"
        )

    for row in rows:
        if row["request_id"] is None or row["request_id"] == "":
            raise EmptyRequestCleanupError(
                f"empty request_id for usage_ledger id {row['usage_id']}"
            )
        if row["log_id"] != row["usage_id"]:
            raise EmptyRequestCleanupError(
                "request_logs ids do not match usage_ledger ids; refusing to delete"
            )
        for mapping_field in _MAPPING_FIELDS:
            usage_value = row[f"usage_{mapping_field}"]
            log_value = (
                row["log_request_model"]
                if mapping_field == "model"
                else row[f"log_{mapping_field}"]
            )
            if usage_value != log_value:
                raise EmptyRequestCleanupError(
                    "request_logs field mismatch for id "
                    f"{row['usage_id']}: {mapping_field}"
                )
        if row["usage_model"] != row["log_model"]:
            raise EmptyRequestCleanupError(
                f"request_logs field mismatch for id {row['usage_id']}: model"
            )
        if int(row["log_success"] or 0) != 1:
            raise EmptyRequestCleanupError(
                f"request_logs row {row['usage_id']} is not successful"
            )


def _related_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _RELATED_TABLES:
        columns = set(_table_columns(connection, table))
        if not columns:
            continue
        if "request_id" not in columns:
            raise EmptyRequestCleanupError(
                f"{table} is missing required column: request_id"
            )
        counts[table] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
                "WHERE request_id IN "
                "(SELECT request_id FROM _rotor_empty_candidates)"
            ).fetchone()[0]
        )
    return counts


def _default_archive_path(source_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return source_path.with_name(
        f"{source_path.stem}.empty-requests-{timestamp}.sqlite"
    )


def _check_archive_target(source_path: Path, archive_path: Path) -> None:
    if archive_path.resolve() == source_path.resolve():
        raise EmptyRequestCleanupError("archive path must differ from database path")
    if not archive_path.parent.exists():
        raise EmptyRequestCleanupError(
            f"archive directory does not exist: {archive_path.parent}"
        )
    if archive_path.exists():
        raise EmptyRequestCleanupError(
            f"archive already exists and will not be overwritten: {archive_path}"
        )


def _reserve_archive_path(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise EmptyRequestCleanupError(
            f"archive already exists and will not be overwritten: {path}"
        ) from exc
    except OSError as exc:
        raise EmptyRequestCleanupError(f"cannot create archive {path}: {exc}") from exc
    else:
        os.close(descriptor)


def _remove_reserved_archive(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_archive(
    source: sqlite3.Connection,
    archive_path: Path,
    candidates: list[tuple[int, str]],
    related_counts: dict[str, int],
    source_path: Path,
) -> None:
    archive = sqlite3.connect(archive_path)
    archive.row_factory = sqlite3.Row
    try:
        archive.execute(
            "CREATE TABLE cleanup_metadata ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        metadata = {
            "format_version": "1",
            "source_database": str(source_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "predicate": (
                "success/missing/zero openai_chat gpt-5.6-sol channel 7"
            ),
            "predicate_sql": " ".join(EMPTY_REQUEST_PREDICATE.split()),
            "usage_ledger_count": str(len(candidates)),
            "request_logs_count": str(len(candidates)),
        }
        archive.executemany(
            "INSERT INTO cleanup_metadata (key, value) VALUES (?, ?)",
            metadata.items(),
        )

        for table in ("request_logs", "usage_ledger", *_RELATED_TABLES):
            if table in related_counts or table in {"request_logs", "usage_ledger"}:
                _copy_table(source, archive, table)

        archive.execute(
            "CREATE TABLE empty_request_map ("
            "usage_ledger_id INTEGER PRIMARY KEY, "
            "request_log_id INTEGER NOT NULL, request_id TEXT NOT NULL)"
        )
        archive.executemany(
            "INSERT INTO empty_request_map "
            "(usage_ledger_id, request_log_id, request_id) VALUES (?, ?, ?)",
            ((ledger_id, ledger_id, request_id) for ledger_id, request_id in candidates),
        )
        archive.commit()

        map_count = archive.execute(
            "SELECT COUNT(*) FROM empty_request_map"
        ).fetchone()[0]
        if int(map_count) != len(candidates):
            raise EmptyRequestCleanupError("archive mapping count mismatch")
        for table, expected in {
            "request_logs": len(candidates),
            "usage_ledger": len(candidates),
            **related_counts,
        }.items():
            actual = archive.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
            if int(actual) != expected:
                raise EmptyRequestCleanupError(
                    f"archive row count mismatch for {table}: {actual} != {expected}"
                )
    except Exception:
        archive.rollback()
        raise
    finally:
        archive.close()


def _copy_table(
    source: sqlite3.Connection,
    archive: sqlite3.Connection,
    table: str,
) -> None:
    schema_row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if schema_row is None or not schema_row[0]:
        raise EmptyRequestCleanupError(f"cannot archive missing table: {table}")
    archive.execute(str(schema_row[0]))
    columns = _table_columns(source, table)
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _column in columns)
    where = (
        "WHERE id IN (SELECT ledger_id FROM _rotor_empty_candidates)"
        if table in {"request_logs", "usage_ledger"}
        else "WHERE request_id IN "
        "(SELECT request_id FROM _rotor_empty_candidates)"
    )
    order = " ORDER BY id" if "id" in columns else ""
    cursor = source.execute(
        f"SELECT {quoted_columns} FROM {_quote_identifier(table)} {where}{order}"
    )
    insert_sql = (
        f"INSERT INTO {_quote_identifier(table)} ({quoted_columns}) "
        f"VALUES ({placeholders})"
    )
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        archive.executemany(insert_sql, (tuple(row) for row in rows))


def _delete_selected_rows(connection: sqlite3.Connection) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for table in (*_RELATED_TABLES, "request_logs", "usage_ledger"):
        if table not in {"request_logs", "usage_ledger"} and not _table_columns(
            connection, table
        ):
            continue
        before = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
                + (
                    "WHERE id IN (SELECT ledger_id FROM _rotor_empty_candidates)"
                    if table == "request_logs" or table == "usage_ledger"
                    else "WHERE request_id IN "
                    "(SELECT request_id FROM _rotor_empty_candidates)"
                )
            ).fetchone()[0]
        )
        connection.execute(
            f"DELETE FROM {_quote_identifier(table)} "
            + (
                "WHERE id IN (SELECT ledger_id FROM _rotor_empty_candidates)"
                if table == "request_logs" or table == "usage_ledger"
                else "WHERE request_id IN "
                "(SELECT request_id FROM _rotor_empty_candidates)"
            )
        )
        after = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
                + (
                    "WHERE id IN (SELECT ledger_id FROM _rotor_empty_candidates)"
                    if table == "request_logs" or table == "usage_ledger"
                    else "WHERE request_id IN "
                    "(SELECT request_id FROM _rotor_empty_candidates)"
                )
            ).fetchone()[0]
        )
        deleted[table] = before - after
    return deleted


def _validate_delete_counts(
    deleted: dict[str, int],
    related: dict[str, int],
    candidate_count: int,
) -> None:
    expected = {
        "request_logs": candidate_count,
        "usage_ledger": candidate_count,
        **related,
    }
    for table, count in expected.items():
        if deleted.get(table) != count:
            raise EmptyRequestCleanupError(
                f"delete count mismatch for {table}: "
                f"{deleted.get(table, 0)} != {count}"
            )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
