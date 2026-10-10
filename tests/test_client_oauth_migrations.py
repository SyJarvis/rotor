import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from rotor.migrations.runner import LegacyDatabaseError, run_startup_migrations


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "f2a3b4c5d6e7"
HEAD = "b4c5d6e7f8a9"
TABLES = {"client_oauth_codes", "client_oauth_grants", "client_oauth_credentials"}


def alembic(path, *arguments):
    result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("unversioned", [False, True])
def test_client_oauth_upgrades_previous_release_and_preserves_api_keys(tmp_path, unversioned):
    path = tmp_path / "previous.db"
    alembic(path, "upgrade", PREVIOUS)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text("INSERT INTO tokens (key,name,enabled,expired,used_quota,[group],request_count,token_count) VALUES ('sk-preserved','saved',1,0,123,'default',0,0)"))
        if unversioned:
            db.execute(text("DROP TABLE alembic_version"))
    backup = run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert bool(backup) is unversioned
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert db.execute(text("SELECT key,used_quota FROM tokens")).one() == ("sk-preserved", 123)
        assert TABLES <= set(inspect(db).get_table_names())
    if backup:
        backup_engine = create_engine(f"sqlite:///{backup}")
        with backup_engine.connect() as db:
            assert not TABLES & set(inspect(db).get_table_names())
            assert db.execute(text("SELECT key,used_quota FROM tokens")).one() == ("sk-preserved", 123)
        backup_engine.dispose()
    engine.dispose()
    alembic(path, "check")


@pytest.mark.parametrize("missing", sorted(TABLES))
def test_partial_unversioned_client_oauth_schema_is_rejected_without_mutation(tmp_path, missing):
    path = tmp_path / "partial.db"
    alembic(path, "upgrade", "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text(f"DROP TABLE {missing}"))
        db.execute(text("DROP TABLE alembic_version"))
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))
    engine.dispose()


def test_client_oauth_downgrade_round_trip_preserves_existing_token_data(tmp_path):
    path = tmp_path / "downgrade.db"
    alembic(path, "upgrade", "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text("INSERT INTO tokens (key,name,enabled,expired,used_quota,[group],request_count,token_count) VALUES ('sk-preserved','saved',1,0,321,'default',0,0)"))
    alembic(path, "downgrade", PREVIOUS)
    with engine.connect() as db:
        assert not TABLES & set(inspect(db).get_table_names())
        assert db.execute(text("SELECT key,used_quota FROM tokens")).one() == ("sk-preserved", 321)
    engine.dispose()
    alembic(path, "upgrade", "head")
    alembic(path, "check")


def test_complete_but_malformed_unversioned_client_oauth_schema_is_rejected(tmp_path):
    path = tmp_path / "malformed.db"
    alembic(path, "upgrade", "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text("ALTER TABLE client_oauth_codes ADD COLUMN unknown_column TEXT"))
        db.execute(text("DROP TABLE alembic_version"))
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert not list(tmp_path.glob("*.bak"))
    engine.dispose()


def test_unversioned_client_oauth_missing_index_is_rejected_without_adoption(tmp_path):
    path = tmp_path / "missing-index.db"
    alembic(path, "upgrade", "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text("DROP INDEX ix_client_oauth_grants_owner_id"))
        db.execute(text("DROP TABLE alembic_version"))
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))
    engine.dispose()
