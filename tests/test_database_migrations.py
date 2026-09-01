import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text

from rotor.database import Base, _ensure_usage_fact_columns
from rotor.migrations.runner import (
    LegacyDatabaseError,
    run_startup_migrations,
)
import rotor.main  # noqa: F401  # Register every model in Base.metadata.


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "b8c9d0e1f2a3"
HEAD_REVISION = "c9d0e1f2a3b4"
ADMIN_TABLES = {
    "admin_auth_events",
    "admin_login_throttles",
    "admin_sessions",
    "admin_users",
}


def _database_url(database_path: Path) -> str:
    return f"sqlite+aiosqlite:///{database_path}"


def _run_alembic(database_path: Path, *arguments: str) -> None:
    environment = {
        **os.environ,
        "DATABASE_URL": _database_url(database_path),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_usage_fact_columns_are_added_to_legacy_tables_idempotently() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE usage_ledger (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text(
            "CREATE TABLE request_logs (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text("INSERT INTO usage_ledger (id) VALUES (1)"))

        _ensure_usage_fact_columns(connection)
        _ensure_usage_fact_columns(connection)

        usage_columns = {
            column["name"]
            for column in inspect(connection).get_columns("usage_ledger")
        }
        log_columns = {
            column["name"]
            for column in inspect(connection).get_columns("request_logs")
        }
        legacy_defaults = connection.execute(text(
            "SELECT usage_schema_version, currency, cost_status, "
            "cache_scope, capacity_scope, billing_scope "
            "FROM usage_ledger WHERE id = 1"
        )).one()

    expected = {
        "uncached_input_tokens",
        "cache_write_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "usage_schema_version",
        "capacity_snapshot",
        "cache_scope",
        "capacity_scope",
        "billing_scope",
        "currency",
        "cost_status",
        "tariff_version",
        "tariff_period",
        "tariff_snapshot",
    }
    assert expected <= usage_columns
    assert expected <= log_columns
    assert legacy_defaults == ("1", "USD", "unknown", None, None, None)


def test_alembic_head_matches_model_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"

    _run_alembic(database_path, "upgrade", "head")
    _run_alembic(database_path, "check")


def test_schema_parity_migration_accepts_legacy_startup_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _run_alembic(database_path, "upgrade", PREVIOUS_REVISION)
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO usage_ledger ("
            "request_id, model, request_protocol, prompt_tokens, "
            "completion_tokens, total_tokens, cached_tokens, reasoning_tokens, "
            "input_audio_tokens, output_audio_tokens, usage_source, input_cost, "
            "output_cost, total_cost, currency, status"
            ") VALUES ("
            "'legacy-request', 'legacy-model', 'openai_chat', 1, 2, 3, 0, 0, "
            "0, 0, 'provider', 0, 0, 0, 'USD', 'success'"
            ")"
        ))
        _ensure_usage_fact_columns(connection)
    engine.dispose()

    _run_alembic(database_path, "upgrade", "head")
    _run_alembic(database_path, "check")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        legacy_row = connection.execute(text(
            "SELECT request_id, total_tokens, usage_schema_version, cost_status "
            "FROM usage_ledger WHERE request_id = 'legacy-request'"
        )).one()
    engine.dispose()

    assert legacy_row == ("legacy-request", 3, "1", "unknown")


def test_startup_migrations_create_empty_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.db"

    backup_path = run_startup_migrations(_database_url(database_path))

    assert backup_path is None
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        revision = connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        ))
    engine.dispose()
    assert revision == HEAD_REVISION
    _run_alembic(database_path, "check")


def test_startup_migrations_upgrade_versioned_database(tmp_path: Path) -> None:
    database_path = tmp_path / "versioned.db"
    _run_alembic(database_path, "upgrade", PREVIOUS_REVISION)

    backup_path = run_startup_migrations(_database_url(database_path))

    assert backup_path is None
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        revision = connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        ))
    engine.dispose()
    assert revision == HEAD_REVISION
    _run_alembic(database_path, "check")


def test_startup_migrations_adopt_current_unversioned_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "current-unversioned.db"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            Base.metadata.tables["usage_ledger"].insert().values(
                request_id="current-request",
                model="current-model",
                request_protocol="openai_chat",
            )
        )
    engine.dispose()

    backup_path = run_startup_migrations(_database_url(database_path))

    assert backup_path is not None
    assert backup_path.exists()
    backup_engine = create_engine(f"sqlite:///{backup_path}")
    with backup_engine.connect() as connection:
        assert "alembic_version" not in inspect(connection).get_table_names()
        assert connection.scalar(text(
            "SELECT request_id FROM usage_ledger "
            "WHERE request_id = 'current-request'"
        )) == "current-request"
    backup_engine.dispose()
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        revision = connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        ))
    engine.dispose()
    assert revision == HEAD_REVISION
    _run_alembic(database_path, "check")


def test_startup_migrations_adopt_pre_admin_legacy_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-current.db"
    legacy_metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in ADMIN_TABLES:
            table.to_metadata(legacy_metadata)
    engine = create_engine(f"sqlite:///{database_path}")
    legacy_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            legacy_metadata.tables["usage_ledger"].insert().values(
                request_id="preserved-request",
                model="legacy-model",
                request_protocol="openai_chat",
            )
        )
    engine.dispose()

    backup_path = run_startup_migrations(_database_url(database_path))

    assert backup_path is not None
    assert backup_path.exists()
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        ))
        preserved_request = connection.scalar(text(
            "SELECT request_id FROM usage_ledger "
            "WHERE request_id = 'preserved-request'"
        ))
    engine.dispose()
    assert ADMIN_TABLES <= tables
    assert revision == HEAD_REVISION
    assert preserved_request == "preserved-request"
    _run_alembic(database_path, "check")


def test_startup_migrations_reject_unknown_legacy_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unknown.db"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE channels (id INTEGER PRIMARY KEY)"))
    engine.dispose()

    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(_database_url(database_path))

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert "alembic_version" not in inspect(connection).get_table_names()
    engine.dispose()
    assert list(tmp_path.glob("unknown.db.pre-alembic-*.bak")) == []
