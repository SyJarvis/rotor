import asyncio
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Integer, MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from rotor.api.client_oauth import router as oauth_router
from rotor.core.deps import get_current_token
from rotor.core.exceptions import APIRouterException, api_router_exception_handler
from rotor.database import create_database_engine, get_db
from rotor.migrations.runner import LegacyDatabaseError, run_startup_migrations
from rotor.models.admin_auth import AdminUser
from rotor.models.token import Token
from rotor.services.client_oauth import pkce_challenge, secret_hash, utc_now


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "f2a3b4c5d6e7"
HEAD = "c5d6e7f8a9b0"
ACCOUNT_FOUNDATION = "b4c5d6e7f8a9"
TABLES = {"client_oauth_codes", "client_oauth_grants", "client_oauth_credentials"}


def alembic(path, *arguments, succeeds=True):
    result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"}, capture_output=True, text=True)
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    return result.stdout + result.stderr


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


def seed_old_oauth(engine):
    with Session(engine) as db:
        db.add(AdminUser(id=1, username="migration-owner", password_hash="migration-password-hash", enabled=True,
            must_change_password=False, quota=None, used_quota=17, allowed_channels=None, allowed_models=["model-a"], default_token_id=1))
        db.add(Token(id=1, key="sk-old-binding", name="old", owner_id=1, enabled=False, quota=0))
        db.commit()
    now = utc_now().replace(tzinfo=None)
    meta = MetaData()
    codes = Table("client_oauth_codes", meta, autoload_with=engine)
    grants = Table("client_oauth_grants", meta, autoload_with=engine)
    credentials = Table("client_oauth_credentials", meta, autoload_with=engine)
    signature = secret_hash("migration-password-hash")
    with engine.begin() as db:
        db.execute(codes.insert(), [dict(id=10 + index, secret_hash=secret_hash(secret), owner_id=1, token_id=1,
            client_id="rotor-client", redirect_uri="http://127.0.0.1:1455/auth/callback", code_challenge=pkce_challenge("v" * 64),
            account_auth_signature=signature, created_at=now, expires_at=now + timedelta(minutes=5),
            used_at=now if index == 1 else None) for index, secret in enumerate(("rotor-code-pending", "rotor-code-used"))])
        db.execute(grants.insert(), [dict(id=20 + index, owner_id=1, token_id=1, client_id="rotor-client", scope="inference",
            account_auth_signature=signature, created_at=now, expires_at=now + timedelta(days=30), last_used_at=now,
            revoked_at=now if index == 1 else None) for index in range(2)])
        db.execute(credentials.insert(), [dict(id=30 + index, grant_id=21 if index == 2 else 20,
            access_hash=secret_hash(access), refresh_hash=secret_hash(refresh), created_at=now,
            access_expires_at=now + timedelta(hours=1), rotated_at=now if index == 1 else None)
            for index, (access, refresh) in enumerate((("rotor-access-active", "rotor-refresh-active"),
                ("rotor-access-previous", "rotor-refresh-previous"), ("rotor-access-revoked", "rotor-refresh-revoked")))])


def oauth_snapshot(db):
    result = {}
    for name in sorted(TABLES):
        table = Table(name, MetaData(), autoload_with=db)
        columns = [column for column in table.columns if column.name != "token_id"]
        result[name] = [tuple(row) for row in db.execute(select(*columns).order_by(table.c.id))]
    return result


async def exercise_migrated_credentials(path):
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(oauth_router)
    app.add_exception_handler(APIRouterException, api_router_exception_handler)

    @app.get("/inference")
    async def inference(principal=Depends(get_current_token)):
        return {"owner_id": principal.owner_id, "token_id": principal.id}

    async def db_dependency():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    app.dependency_overrides[get_db] = db_dependency
    try:
        async with sessions() as db:
            await db.execute(text("DELETE FROM tokens"))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.get("/inference", headers={"Authorization": "Bearer rotor-access-active"})
            assert response.status_code == 200 and response.json() == {"owner_id": 1, "token_id": None}
            response = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client", "refresh_token": "rotor-refresh-active"})
            assert response.status_code == 200, response.text
            refreshed = response.json()
            assert (await client.get("/inference", headers={"Authorization": "Bearer " + refreshed["access_token"]})).json() == {"owner_id": 1, "token_id": None}
            exchange = await client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "rotor-client", "code": "rotor-code-pending",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback", "code_verifier": "v" * 64})
            assert exchange.status_code == 200, exchange.text
            used = await client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "rotor-client", "code": "rotor-code-used",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback", "code_verifier": "v" * 64})
            assert used.status_code == 400 and used.json()["error"] == "invalid_grant"
            revoked = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client", "refresh_token": "rotor-refresh-revoked"})
            assert revoked.status_code == 400 and revoked.json()["error"] == "invalid_grant"
            replay = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "rotor-client", "refresh_token": "rotor-refresh-previous"})
            assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
            assert (await client.get("/inference", headers={"Authorization": "Bearer " + refreshed["access_token"]})).status_code == 401
        async with sessions() as db:
            assert (await db.scalars(select(Token))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("revision", ["a3b4c5d6e7f8", ACCOUNT_FOUNDATION])
@pytest.mark.parametrize("unversioned", [False, True])
def test_account_oauth_migration_preserves_every_credential_field_and_pending_code(tmp_path, revision, unversioned):
    path = tmp_path / "preserved-oauth.db"
    alembic(path, "upgrade", revision)
    engine = create_engine(f"sqlite:///{path}")
    seed_old_oauth(engine)
    with engine.begin() as db:
        before = oauth_snapshot(db)
        if unversioned:
            db.execute(text("DROP TABLE alembic_version"))
    backup = run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert bool(backup) is unversioned
    with engine.connect() as db:
        assert oauth_snapshot(db) == before
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        for table in ("client_oauth_codes", "client_oauth_grants"):
            assert "token_id" not in {column["name"] for column in inspect(db).get_columns(table)}
            assert all(fk["referred_table"] != "tokens" for fk in inspect(db).get_foreign_keys(table))
    if backup:
        backup_engine = create_engine(f"sqlite:///{backup}")
        with backup_engine.connect() as db:
            assert oauth_snapshot(db) == before
            for table in ("client_oauth_codes", "client_oauth_grants"):
                assert db.scalar(text(f"SELECT token_id FROM {table} LIMIT 1")) == 1
        backup_engine.dispose()
    engine.dispose()
    alembic(path, "check")
    asyncio.run(exercise_migrated_credentials(path))


@pytest.mark.parametrize("missing", sorted(TABLES))
def test_versioned_partial_oauth_schema_is_rejected_before_version_or_ddl_changes(tmp_path, missing):
    path = tmp_path / "versioned-partial.db"
    alembic(path, "upgrade", ACCOUNT_FOUNDATION)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        db.execute(text(f"DROP TABLE {missing}"))
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="partial client OAuth schema"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert path.read_bytes() == before and not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("unversioned", [False, True])
@pytest.mark.parametrize("broken", ["one-binding", "missing-fk", "nullable-binding"])
def test_partial_oauth_key_binding_is_rejected_without_writes(tmp_path, unversioned, broken):
    path = tmp_path / "partial-binding.db"
    alembic(path, "upgrade", ACCOUNT_FOUNDATION)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as db:
        operations = Operations(MigrationContext.configure(db))
        with operations.batch_alter_table("client_oauth_codes", naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}) as batch:
            if broken in {"one-binding", "missing-fk"}:
                batch.drop_constraint("fk_client_oauth_codes_token_id_tokens", type_="foreignkey")
                if broken == "one-binding":
                    batch.drop_column("token_id")
            else:
                batch.alter_column("token_id", existing_type=Integer(), nullable=True)
        if unversioned:
            db.execute(text("DROP TABLE alembic_version"))
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="partial client OAuth Key binding"):
        run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    assert path.read_bytes() == before and not list(tmp_path.glob("*.bak"))


def test_downgrade_with_oauth_data_refuses_before_ddl_and_keeps_credentials(tmp_path):
    path = tmp_path / "cannot-downgrade.db"
    alembic(path, "upgrade", ACCOUNT_FOUNDATION)
    engine = create_engine(f"sqlite:///{path}")
    seed_old_oauth(engine)
    engine.dispose()
    run_startup_migrations(f"sqlite+aiosqlite:///{path}")
    before = path.read_bytes()
    output = alembic(path, "downgrade", ACCOUNT_FOUNDATION, succeeds=False)
    assert "Cannot downgrade account-bound OAuth authorization data" in output
    assert path.read_bytes() == before
