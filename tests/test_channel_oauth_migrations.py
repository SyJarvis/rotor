import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from rotor.migrations.runner import LegacyDatabaseError, run_startup_migrations


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "c9d0e1f2a3b4"
HEAD_REVISION = "c5d6e7f8a9b0"
_INSERT_CHANNEL = text(
    "INSERT INTO channels(name,type,key,base_url,models,model_mapping,priority,weight,enabled,test_only,protocol,extra) "
    "VALUES ('preserved','openai','test-key','https://example.invalid','[]','{}',1,1,1,0,'openai','{}')"
)


def alembic(database_path, *arguments):
    result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
                            env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_oauth_migration_adopts_previous_unversioned_schema(tmp_path):
    path = tmp_path / "previous.db"
    alembic(path, "upgrade", PREVIOUS_REVISION)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(_INSERT_CHANNEL)
        db.execute(text("DROP TABLE alembic_version"))
    backup = run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert backup is not None and backup.exists()
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert db.execute(text("SELECT name,key,auth_type,oauth_access_token,oauth_refresh_token FROM channels")).one() == (
            "preserved", "test-key", "api_key", None, None)
        assert "channel_oauth_sessions" in inspect(db).get_table_names()
    engine.dispose()
    alembic(path, "check")


@pytest.mark.parametrize("revision", ["b8c9d0e1f2a3", "f6a7b8c9d0e1"])
def test_oauth_migration_does_not_guess_unsupported_unversioned_history(tmp_path, revision):
    path = tmp_path / "unsupported.db"
    alembic(path, "upgrade", revision)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(_INSERT_CHANNEL)
        db.execute(text("DROP TABLE alembic_version"))
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    with engine.connect() as db:
        assert "alembic_version" not in inspect(db).get_table_names()
        assert db.execute(text("SELECT name,key FROM channels")).one() == ("preserved", "test-key")
    engine.dispose()
    assert not list(tmp_path.glob("*.bak"))


def test_partial_oauth_unversioned_schema_is_rejected_without_backup(tmp_path):
    path = tmp_path / "partial.db"
    alembic(path, "upgrade", PREVIOUS_REVISION)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text("DROP TABLE alembic_version"))
        db.execute(text("ALTER TABLE channels ADD COLUMN auth_type VARCHAR(20) NOT NULL DEFAULT 'api_key'"))
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    with engine.connect() as db:
        assert "alembic_version" not in inspect(db).get_table_names()
    engine.dispose()
    assert not list(tmp_path.glob("*.bak"))


def test_oauth_migration_downgrade_preserves_api_key_channels(tmp_path):
    path = tmp_path / "downgrade.db"
    alembic(path, "upgrade", "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(_INSERT_CHANNEL)
    alembic(path, "downgrade", PREVIOUS_REVISION)
    with engine.connect() as db:
        assert "channel_oauth_sessions" not in inspect(db).get_table_names()
        assert "auth_type" not in {column["name"] for column in inspect(db).get_columns("channels")}
        assert db.scalar(text("SELECT key FROM channels")) == "test-key"
    engine.dispose()
    alembic(path, "upgrade", "head")
    alembic(path, "check")


@pytest.mark.parametrize("unversioned", [False, True])
def test_browser_migration_cancels_device_sessions_and_preserves_channel_credentials(tmp_path, unversioned):
    path = tmp_path / "device.db"
    alembic(path, "upgrade", "d0e1f2a3b4c5")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(_INSERT_CHANNEL)
        db.execute(text(
            "INSERT INTO channels(name,type,key,base_url,models,model_mapping,priority,weight,enabled,test_only,protocol,extra,"
            "auth_type,oauth_access_token,oauth_refresh_token,oauth_account_id,oauth_expires_at,oauth_status) "
            "VALUES ('oauth','openai_codex','','https://chatgpt.com/backend-api/codex','[]','{}',1,1,1,0,'openai_responses','{}',"
            "'oauth','persisted-access','persisted-refresh','persisted-account',123456789,'connected')"
        ))
        for identifier, status in (("device-pending", "pending"), ("device-authorized", "authorized")):
            db.execute(text(
                "INSERT INTO channel_oauth_sessions(id,admin_user_id,status,device_auth_id,user_code,interval,expires_at,next_poll_at,"
                "poll_owner,poll_lock_until,access_token,refresh_token,token_expires_at,account_id) "
                "VALUES (:identifier,7,:status,'old-device-secret','OLD-CODE',5,9999999999,0,'old-owner',9999999999,"
                "'temporary-access','temporary-refresh',9999999999,'temporary-account')"
            ), {"identifier": identifier, "status": status})
        if unversioned:
            db.execute(text("DROP TABLE alembic_version"))
    backup = run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert bool(backup) is unversioned
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert db.execute(text("SELECT key,auth_type FROM channels WHERE name='preserved'")).one() == ("test-key", "api_key")
        assert db.execute(text(
            "SELECT auth_type,oauth_access_token,oauth_refresh_token,oauth_account_id,oauth_expires_at,oauth_status FROM channels WHERE name='oauth'"
        )).one() == ("oauth", "persisted-access", "persisted-refresh", "persisted-account", 123456789, "connected")
        for row in db.execute(text(
            "SELECT status,device_auth_id,user_code,poll_owner,poll_lock_until,access_token,refresh_token,token_expires_at,account_id,"
            "oauth_state,pkce_verifier FROM channel_oauth_sessions"
        )):
            assert row[0] == "cancelled" and all(value is None for value in row[1:])
    engine.dispose()
    alembic(path, "check")


def test_partial_browser_unversioned_schema_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "partial-browser.db"
    alembic(path, "upgrade", "d0e1f2a3b4c5")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(_INSERT_CHANNEL)
        db.execute(text("ALTER TABLE channel_oauth_sessions ADD COLUMN oauth_state VARCHAR(64)"))
        db.execute(text("DROP TABLE alembic_version"))
    with pytest.raises(LegacyDatabaseError, match="cannot safely adopt"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    with engine.connect() as db:
        assert "alembic_version" not in inspect(db).get_table_names()
        assert db.scalar(text("SELECT key FROM channels")) == "test-key"
        assert "pkce_verifier" not in {column["name"] for column in inspect(db).get_columns("channel_oauth_sessions")}
    engine.dispose()
    assert not list(tmp_path.glob("*.bak"))
