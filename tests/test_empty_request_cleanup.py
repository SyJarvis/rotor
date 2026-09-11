from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rotor.maintenance.empty_requests import (
    EmptyRequestCleanupError,
    cleanup_empty_requests,
    inspect_empty_requests,
)


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE usage_ledger (
            id INTEGER PRIMARY KEY,
            request_id TEXT NOT NULL,
            token_id INTEGER,
            channel_id INTEGER,
            model TEXT NOT NULL,
            request_protocol TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            uncached_input_tokens INTEGER NOT NULL,
            cached_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_write_5m_tokens INTEGER NOT NULL,
            cache_write_1h_tokens INTEGER NOT NULL,
            usage_source TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT
        );
        CREATE TABLE request_logs (
            id INTEGER PRIMARY KEY,
            token_id INTEGER,
            channel_id INTEGER,
            model TEXT NOT NULL,
            request_model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            uncached_input_tokens INTEGER NOT NULL,
            cached_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_write_5m_tokens INTEGER NOT NULL,
            cache_write_1h_tokens INTEGER NOT NULL,
            success INTEGER NOT NULL,
            payload TEXT
        );
        CREATE TABLE request_attempts (
            id INTEGER PRIMARY KEY,
            request_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload TEXT
        );
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY,
            request_id TEXT NOT NULL,
            payload TEXT
        );
        CREATE TABLE conversation_records (
            id INTEGER PRIMARY KEY,
            request_id TEXT NOT NULL,
            payload TEXT
        );
        """
    )

    def add_pair(row_id: int, request_id: str, *, total_tokens: int = 0) -> None:
        zeroes = (0, 0, total_tokens, 0, 0, 0, 0, 0)
        connection.execute(
            """INSERT INTO usage_ledger
            (id, request_id, token_id, channel_id, model, request_protocol,
             prompt_tokens, completion_tokens, total_tokens,
             uncached_input_tokens, cached_tokens, cache_write_tokens,
             cache_write_5m_tokens, cache_write_1h_tokens, usage_source,
             status, payload)
            VALUES (?, ?, 1, ?, 'gpt-5.6-sol', 'openai_chat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                request_id,
                7 if total_tokens == 0 else 2,
                *zeroes,
                "missing" if total_tokens == 0 else "provider",
                "success",
                f"ledger-{row_id}",
            ),
        )
        connection.execute(
            """INSERT INTO request_logs
            (id, token_id, channel_id, model, request_model,
             prompt_tokens, completion_tokens, total_tokens,
             uncached_input_tokens, cached_tokens, cache_write_tokens,
             cache_write_5m_tokens, cache_write_1h_tokens, success, payload)
            VALUES (?, 1, ?, 'gpt-5.6-sol', 'gpt-5.6-sol', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                row_id,
                7 if total_tokens == 0 else 2,
                *zeroes,
                f"log-{row_id}",
            ),
        )

    # Candidate IDs are deliberately non-contiguous.  The nonzero row must
    # survive even though its id falls between the two incident rows.
    add_pair(10, "req-empty-a")
    add_pair(12, "req-healthy", total_tokens=9)
    add_pair(14, "req-empty-b")
    connection.execute(
        "UPDATE usage_ledger SET channel_id = 2 WHERE request_id = 'req-healthy'"
    )
    connection.execute(
        "INSERT INTO request_attempts VALUES (1, 'req-empty-a', 'success', 'a1')"
    )
    connection.execute(
        "INSERT INTO request_attempts VALUES (2, 'req-empty-a', 'failed', 'a2')"
    )
    connection.execute(
        "INSERT INTO request_attempts VALUES (3, 'req-healthy', 'success', 'h1')"
    )
    connection.execute(
        "INSERT INTO routing_decisions VALUES (1, 'req-empty-a', 'route-a')"
    )
    connection.execute(
        "INSERT INTO routing_decisions VALUES (2, 'req-empty-b', 'route-b')"
    )
    connection.execute(
        "INSERT INTO conversation_records VALUES (1, 'req-empty-a', 'conv-a')"
    )
    connection.execute(
        "INSERT INTO conversation_records VALUES (2, 'req-empty-b', 'conv-b')"
    )
    connection.commit()
    connection.close()


def test_inspect_empty_requests_is_narrow_and_read_only(tmp_path: Path) -> None:
    database = tmp_path / "rotor.db"
    _create_database(database)

    report = inspect_empty_requests(database)

    assert report.matched_usage_ledger == 2
    assert report.matched_request_logs == 2
    assert report.related_rows == {
        "request_attempts": 2,
        "routing_decisions": 2,
        "conversation_records": 2,
    }
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 3
    connection.close()


def test_cleanup_archives_complete_rows_then_removes_success_traces(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rotor.db"
    archive = tmp_path / "empty-requests.sqlite"
    _create_database(database)

    report = cleanup_empty_requests(
        database,
        archive_path=archive,
        apply=True,
        confirm=True,
    )

    assert report.archived_path == archive
    assert report.deleted_rows == {
        "request_attempts": 2,
        "routing_decisions": 2,
        "conversation_records": 2,
        "request_logs": 2,
        "usage_ledger": 2,
    }
    source = sqlite3.connect(database)
    assert source.execute("SELECT id FROM usage_ledger").fetchall() == [(12,),]
    assert source.execute("SELECT id FROM request_logs").fetchall() == [(12,),]
    assert source.execute("SELECT COUNT(*) FROM request_attempts").fetchone()[0] == 1
    source.close()

    saved = sqlite3.connect(archive)
    assert saved.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 2
    assert saved.execute("SELECT payload FROM request_logs WHERE id = 10").fetchone()[0] == "log-10"
    assert saved.execute(
        "SELECT usage_ledger_id, request_log_id, request_id "
        "FROM empty_request_map ORDER BY usage_ledger_id"
    ).fetchall() == [
        (10, 10, "req-empty-a"),
        (14, 14, "req-empty-b"),
    ]
    saved.close()


def test_cleanup_fails_closed_when_request_log_mapping_differs(tmp_path: Path) -> None:
    database = tmp_path / "rotor.db"
    archive = tmp_path / "empty-requests.sqlite"
    _create_database(database)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE request_logs SET channel_id = 99 WHERE id = 10")
    connection.commit()
    connection.close()

    with pytest.raises(EmptyRequestCleanupError, match="field mismatch"):
        cleanup_empty_requests(
            database,
            archive_path=archive,
            apply=True,
            confirm=True,
        )

    assert not archive.exists()
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0] == 3
    connection.close()


def test_cleanup_requires_explicit_confirmation(tmp_path: Path) -> None:
    database = tmp_path / "rotor.db"
    _create_database(database)

    with pytest.raises(EmptyRequestCleanupError, match="explicit confirmation"):
        cleanup_empty_requests(database, apply=True)
