import os
from pathlib import Path
import subprocess
import sys

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import Integer, create_engine, inspect, text
from sqlalchemy.orm import Session

from rotor.database import Base
from rotor.migrations.runner import LegacyDatabaseError, run_startup_migrations
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.token import Token


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "a3b4c5d6e7f8"
FOUNDATION = "b4c5d6e7f8a9"
HEAD = "c5d6e7f8a9b0"
TABLES = ("response_routes", "session_leases", "session_lease_events")


def alembic(path, *arguments, succeeds=True):
    result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"}, capture_output=True, text=True)
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    return result.stdout + result.stderr


def seed_key_state(engine):
    with Session(engine) as db:
        db.add_all([
            AdminUser(id=1, username="alice", password_hash="unused", enabled=True, quota=1000, used_quota=123),
            Token(id=1, key="sk-preserved", name="saved", owner_id=1, used_quota=123),
            Channel(id=1, name="provider", type="openai", key="upstream", base_url="https://provider.invalid/v1"),
        ])
        db.commit()
    with engine.begin() as db:
        db.execute(text("INSERT INTO response_routes (response_id,token_id,channel_id,status,usage_accounted) VALUES ('resp_saved',1,1,'completed',1)"))
        db.execute(text("INSERT INTO session_leases (token_id,session_id,logical_model,channel_id,last_used_at,expires_at) VALUES (1,'session_saved','model-a',1,'2026-10-10 00:00:00','2026-10-11 00:00:00')"))
        db.execute(text("INSERT INTO session_lease_events (lease_id,request_id,token_id,session_id,logical_model,event_type,channel_id,reason) VALUES (1,'req_saved',1,'session_saved','model-a','assigned',1,'first_success')"))


def assert_key_state(db):
    assert db.execute(text("SELECT quota,used_quota FROM admin_users")).one() == (1000, 123)
    assert db.execute(text("SELECT key,owner_id,used_quota FROM tokens")).one() == ("sk-preserved", 1, 123)
    assert db.execute(text("SELECT response_id,token_id,status,usage_accounted FROM response_routes")).one() == ("resp_saved", 1, "completed", 1)
    assert db.execute(text("SELECT token_id,session_id,logical_model,channel_id FROM session_leases")).one() == (1, "session_saved", "model-a", 1)
    assert db.execute(text("SELECT token_id,request_id,event_type FROM session_lease_events")).one() == (1, "req_saved", "assigned")


def assert_account_schema(db):
    schema = inspect(db)
    for table in TABLES:
        columns = {column["name"]: column for column in schema.get_columns(table)}
        assert columns["token_id"]["nullable"] and columns["owner_id"]["nullable"]
        assert isinstance(columns["owner_id"]["type"], Integer)
        assert f"ck_{table}_identity" in {check["name"] for check in schema.get_check_constraints(table)}
        assert f"ix_{table}_owner_id" in {index["name"] for index in schema.get_indexes(table)}
        owner_fks = [fk for fk in schema.get_foreign_keys(table) if fk["constrained_columns"] == ["owner_id"]]
        if table == "session_lease_events":
            assert owner_fks == []
        else:
            assert len(owner_fks) == 1
            assert owner_fks[0]["referred_table"] == "admin_users"
            assert owner_fks[0]["options"]["ondelete"] == "CASCADE"
            expected = "uq_response_routes_owner_response" if table == "response_routes" else "uq_session_leases_owner_session_model"
            assert expected in {constraint["name"] for constraint in schema.get_unique_constraints(table)}


def test_fresh_account_scope_schema_matches_metadata(tmp_path):
    path = tmp_path / "fresh.db"
    assert run_startup_migrations(f"sqlite+aiosqlite:///{path}") is None
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert_account_schema(db)
    engine.dispose()
    alembic(path, "check")


@pytest.mark.parametrize("unversioned", [False, True])
def test_previous_account_scope_upgrade_preserves_key_state_and_backup(tmp_path, unversioned):
    path = tmp_path / "previous.db"
    alembic(path, "upgrade", PREVIOUS)
    engine = create_engine(f"sqlite:///{path}")
    seed_key_state(engine)
    if unversioned:
        with engine.begin() as db:
            db.execute(text("DROP TABLE alembic_version"))
    backup = run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert bool(backup) is unversioned
    with engine.connect() as db:
        assert_key_state(db)
        assert_account_schema(db)
        assert all(db.scalar(text(f"SELECT owner_id FROM {table}")) is None for table in TABLES)
    if backup:
        saved = create_engine(f"sqlite:///{backup}")
        with saved.connect() as db:
            assert_key_state(db)
            assert all("owner_id" not in {column["name"] for column in inspect(db).get_columns(table)} for table in TABLES)
        saved.dispose()
    engine.dispose()
    alembic(path, "check")


@pytest.mark.parametrize("metadata_clone", [False, True])
def test_key_only_account_schema_downgrade_round_trip_including_unnamed_foreign_keys(tmp_path, metadata_clone):
    path = tmp_path / "round-trip.db"
    engine = create_engine(f"sqlite:///{path}")
    if metadata_clone:
        Base.metadata.create_all(engine)
        assert run_startup_migrations(f"sqlite+aiosqlite:///{path}") is not None
    else:
        alembic(path, "upgrade", HEAD)
    seed_key_state(engine)
    alembic(path, "downgrade", PREVIOUS)
    with engine.connect() as db:
        assert_key_state(db)
        for table in TABLES:
            columns = {column["name"]: column for column in inspect(db).get_columns(table)}
            assert "owner_id" not in columns and not columns["token_id"]["nullable"]
    alembic(path, "upgrade", HEAD)
    with engine.connect() as db:
        assert_key_state(db)
        assert_account_schema(db)
    engine.dispose()
    alembic(path, "check")


@pytest.mark.parametrize("broken", ["owner-column-only", "nullable-token-only", "route-fk", "lease-fk", "index", "check", "unique"])
def test_partial_unversioned_account_schema_is_rejected_without_writes_or_backup(tmp_path, broken):
    path = tmp_path / "partial.db"
    alembic(path, "upgrade", PREVIOUS if broken in {"owner-column-only", "nullable-token-only"} else HEAD)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        operations = Operations(MigrationContext.configure(db))
        if broken == "owner-column-only":
            db.execute(text("ALTER TABLE response_routes ADD COLUMN owner_id INTEGER"))
        elif broken == "nullable-token-only":
            with operations.batch_alter_table("response_routes") as batch:
                batch.alter_column("token_id", existing_type=Integer(), nullable=True)
        elif broken == "index":
            db.execute(text("DROP INDEX ix_session_lease_events_owner_id"))
        else:
            table = "session_leases" if broken == "lease-fk" else "response_routes"
            with operations.batch_alter_table(table) as batch:
                if broken.endswith("-fk"):
                    batch.drop_constraint(f"fk_{table}_owner_id_admin_users", type_="foreignkey")
                elif broken == "check":
                    batch.drop_constraint("ck_response_routes_identity", type_="check")
                else:
                    batch.drop_constraint("uq_response_routes_owner_response", type_="unique")
        db.execute(text("DROP TABLE alembic_version"))
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="partial account identity"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("account_table", TABLES)
def test_downgrade_refuses_account_owned_state_before_ddl(tmp_path, account_table):
    path = tmp_path / "account-state.db"
    alembic(path, "upgrade", FOUNDATION)
    engine = create_engine(f"sqlite:///{path}")
    seed_key_state(engine)
    with engine.begin() as db:
        db.execute(text(f"UPDATE {account_table} SET token_id=NULL,owner_id=1"))
    engine.dispose()
    before = path.read_bytes()
    output = alembic(path, "downgrade", PREVIOUS, succeeds=False)
    assert "Cannot downgrade account-owned state" in output
    assert path.read_bytes() == before
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == FOUNDATION
        assert_account_schema(db)
        assert db.scalar(text(f"SELECT owner_id FROM {account_table}")) == 1
    engine.dispose()
