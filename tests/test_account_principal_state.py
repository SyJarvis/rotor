import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rotor.core.client_session import ClientSessionContext, build_responses_prompt_cache_key
from rotor.core.user_access import (
    AccountPrincipal,
    accessible_models,
    principal_scope_key,
    reload_principal,
    resolve_account_principal,
    validate_account_access,
)
from rotor.database import Base, create_database_engine
from rotor.gateway.accounting import AccountingService
from rotor.models.admin_auth import AdminUser
from rotor.models.channel import Channel
from rotor.models.response_route import ResponseRoute
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLease, SessionLeaseEvent
from rotor.models.token import Token
from rotor.services.response_routes import get_response_route, save_response_route
from rotor.services.session_leases import get_preferred_channel_id, record_session_lease_success


@asynccontextmanager
async def database_scope(tmp_path):
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            AdminUser(id=1, username="alice", password_hash="unused", quota=1000,
                      enabled=True, must_change_password=False, allowed_channels=[1], allowed_models=["model-a"]),
            AdminUser(id=2, username="bob", password_hash="unused", quota=1000,
                      enabled=True, must_change_password=False, allowed_channels=[2], allowed_models=["model-a"]),
            Token(id=1, owner_id=1, key="sk-explicit-owned-key", name="restricted",
                  allowed_channels=[2], allowed_models=["model-a"]),
            Channel(id=1, name="first", type="openai", key="upstream", base_url="https://first.invalid/v1", models=["model-a", "hidden-model"]),
            Channel(id=2, name="second", type="openai", key="upstream", base_url="https://second.invalid/v1", models=["model-a"]),
        ])
        await db.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


def test_account_principal_is_immutable_and_has_distinct_affinity_and_cache_scope():
    first, second = AccountPrincipal(1), AccountPrincipal(2)
    assert not isinstance(first, Token) and first.id is None and first.user_id is None
    with pytest.raises(FrozenInstanceError):
        first.owner_id = 2
    key = Token(id=1)
    assert principal_scope_key(key) == 1
    assert principal_scope_key(first) == "account:1"
    assert principal_scope_key(second) == "account:2"
    session = ClientSessionContext(session_id="same-session", source="x-rotor-session-id")
    assert session.affinity_key(principal_scope_key(key)) == "1:same-session"
    assert session.affinity_key(principal_scope_key(first)) == "account:1:same-session"
    scopes = [principal_scope_key(principal) for principal in (key, first, second)]
    caches = [build_responses_prompt_cache_key(token_id=scope, model="model-a", session_id="same-session") for scope in scopes]
    assert len(set(caches)) == 3
    assert caches[0] == "rotor_" + hashlib.sha256(b"1\0model-a\0same-session").hexdigest()[:58]
    with pytest.raises(ValueError, match="persistent ID"):
        principal_scope_key(Token())


def test_account_helpers_ignore_default_key_permissions_and_reload_for_finished_usage(tmp_path):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            async with sessions() as db:
                principal = await resolve_account_principal(db, 1, model="model-a")
                assert principal == AccountPrincipal(1)
                assert (await validate_account_access(1, db)).default_token_id is None
                assert await accessible_models(principal, db) == ["model-a"]
                key = await db.get(Token, 1)
                assert await accessible_models(key, db) == []
                with pytest.raises(HTTPException) as rejected:
                    await resolve_account_principal(db, 1, model="hidden-model")
                assert rejected.value.status_code == 403
            async with sessions() as writer:
                await writer.execute(update(AdminUser).where(AdminUser.id == 1).values(enabled=False, quota=0))
                await writer.commit()
            async with sessions() as db:
                assert await reload_principal(db, principal) is principal
                assert (await reload_principal(db, key)).id == key.id
                with pytest.raises(HTTPException) as rejected:
                    await resolve_account_principal(db, 1)
                assert rejected.value.status_code == 403
                await db.execute(delete(AdminUser).where(AdminUser.id == 2))
                await db.commit()
                assert await reload_principal(db, AccountPrincipal(2)) is None
    asyncio.run(exercise())


def test_account_state_and_key_state_are_separate_with_identical_response_and_session_ids(tmp_path):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            now = datetime.now(timezone.utc)
            scopes = [(1, None, 1), (2, None, 2), (None, 1, 1)]
            async with sessions() as db:
                for owner_id, token_id, channel_id in scopes:
                    await save_response_route(db, response_id="resp_shared", owner_id=owner_id,
                        token_id=token_id, channel_id=channel_id, model="model-a", status="completed")
                    await record_session_lease_success(db, request_id=f"req-{owner_id}-{token_id}",
                        owner_id=owner_id, token_id=token_id, session_id="same-session", logical_model="model-a",
                        channel_id=channel_id, idle_ttl_seconds=900, now=now)
                await db.commit()
                for owner_id, token_id, channel_id in scopes:
                    route = await get_response_route(db, "resp_shared", token_id, owner_id=owner_id)
                    assert (route.owner_id, route.token_id, route.channel_id) == (owner_id, token_id, channel_id)
                    assert await get_preferred_channel_id(db, owner_id=owner_id, token_id=token_id,
                        session_id="same-session", logical_model="model-a", now=now + timedelta(minutes=1)) == channel_id
                events = (await db.scalars(select(SessionLeaseEvent))).all()
                assert {(event.owner_id, event.token_id, event.channel_id) for event in events} == set(scopes)
                await db.execute(delete(AdminUser).where(AdminUser.id == 2))
                await db.commit()
                assert await get_response_route(db, "resp_shared", owner_id=2) is None
                assert await get_preferred_channel_id(db, owner_id=2, session_id="same-session", logical_model="model-a") is None
                assert (await db.scalars(select(SessionLeaseEvent).where(SessionLeaseEvent.owner_id == 2))).one().request_id == "req-2-None"
                assert await get_response_route(db, "resp_shared", 1) is not None
    asyncio.run(exercise())


@pytest.mark.parametrize("token_id,owner_id", [(None, None), (1, 1)])
@pytest.mark.parametrize("model", [ResponseRoute, SessionLease, SessionLeaseEvent])
def test_database_enforces_exactly_one_principal_scope(tmp_path, model, token_id, owner_id):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            now = datetime.now(timezone.utc)
            if model is ResponseRoute:
                fields = {"response_id": "resp_bad", "channel_id": 1}
            elif model is SessionLease:
                fields = {"session_id": "bad", "logical_model": "model-a", "channel_id": 1,
                          "last_used_at": now, "expires_at": now + timedelta(minutes=15)}
            else:
                fields = {"lease_id": 1, "request_id": "req_bad", "session_id": "bad",
                          "logical_model": "model-a", "event_type": "assigned", "reason": "first_success"}
            async with sessions() as db:
                db.add(model(token_id=token_id, owner_id=owner_id, **fields))
                with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                    await db.commit()
                await db.rollback()
    asyncio.run(exercise())


@pytest.mark.parametrize("model", [ResponseRoute, SessionLease])
def test_database_enforces_account_uniqueness_when_token_id_is_null(tmp_path, model):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            now = datetime.now(timezone.utc)
            fields = {"response_id": "same-response"} if model is ResponseRoute else {
                "session_id": "same-session", "logical_model": "model-a",
                "last_used_at": now, "expires_at": now + timedelta(minutes=15)}
            async with sessions() as db:
                db.add_all([model(owner_id=1, channel_id=1, **fields), model(owner_id=1, channel_id=2, **fields)])
                with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                    await db.commit()
                await db.rollback()
    asyncio.run(exercise())


def test_state_service_rejects_missing_or_ambiguous_scope(tmp_path):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            async with sessions() as db:
                for scope in ({}, {"token_id": 1, "owner_id": 1}):
                    with pytest.raises(ValueError, match="exactly one"):
                        await get_response_route(db, "resp_shared", **scope)
                    with pytest.raises(ValueError, match="exactly one"):
                        await get_preferred_channel_id(db, session_id="same-session", logical_model="model-a", **scope)
    asyncio.run(exercise())


def test_routing_snapshot_overrides_untrusted_account_identity(tmp_path):
    async def exercise():
        async with database_scope(tmp_path) as sessions:
            async with sessions() as db:
                channel = await db.get(Channel, 1)
                decision = SimpleNamespace(candidates=[channel], strategy="fallback_order", scores=None)
                AccountingService().record_routing_decision(db, request_id="req_trusted", token=AccountPrincipal(1),
                    model="model-a", request_protocol="openai_chat", decision=decision, features={"owner_id": 2})
                await db.commit()
                record = (await db.scalars(select(RoutingDecisionRecord))).one()
                assert record.token_id is None and record.feature_snapshot["owner_id"] == 1
    asyncio.run(exercise())
