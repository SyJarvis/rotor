import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.database import Base
from rotor.models.request_attempt import RequestAttempt
from rotor.models.session_lease import SessionLease, SessionLeaseEvent
from rotor.models.token import Token  # noqa: F401
from rotor.services.session_leases import (
    REASSESSMENT_DEFERRED_REASON,
    get_session_lease_preference,
    record_session_lease_success,
)


START = datetime(2026, 9, 10, tzinfo=timezone.utc)
CHANNELS = [
    SimpleNamespace(id=1, protocol="openai"),
    SimpleNamespace(id=2, protocol="anthropic"),
]


@asynccontextmanager
async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db, engine
    finally:
        await engine.dispose()


async def _success(
    db,
    request_id,
    seconds,
    *,
    channel_id=2,
    attempt_index=1,
    request_protocol="openai_chat",
    provider_protocol="anthropic",
    reason="fallback_success",
):
    now = START + timedelta(seconds=seconds)
    db.add(RequestAttempt(
        request_id=request_id,
        attempt_index=attempt_index,
        channel_id=channel_id,
        requested_model="model-a",
        request_protocol=request_protocol,
        provider_protocol=provider_protocol,
        outcome="success",
        started_at=now,
        finished_at=now,
    ))
    result = await record_session_lease_success(
        db,
        request_id=request_id,
        token_id=7,
        session_id="session-a",
        logical_model="model-a",
        channel_id=channel_id,
        idle_ttl_seconds=1800,
        migration_reason=reason,
        now=now,
    )
    await db.commit()
    return result


async def _preference(db, seconds, **overrides):
    return await get_session_lease_preference(db, **{
        "token_id": 7,
        "session_id": "session-a",
        "logical_model": "model-a",
        "request_protocol": "openai_chat",
        "channels": CHANNELS,
        "active_native_channel_ids": {1},
        "now": START + timedelta(seconds=seconds),
        **overrides,
    })


def test_active_renewals_do_not_move_fallback_reassessment_deadline():
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "first-fallback", 0)
            # The legacy first-success event is attributable through its attempt.
            anchor = (await db.scalars(select(SessionLeaseEvent))).one()
            assert anchor.reason == "first_success"
            await _success(db, "normal-renewal", 250, attempt_index=0, reason="request_success")
            assert not (await _preference(db, 299)).reassessment_due
            preference = await _preference(db, 300)
            assert preference.channel_id == 2
            assert preference.reassessment_due
            assert not db.new and not db.dirty
            assert len((await db.scalars(select(SessionLeaseEvent))).all()) == 1
    asyncio.run(exercise())


def test_failed_reassessment_checkpoint_sets_a_new_fixed_deadline():
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "first-fallback", 0)
            await _success(db, "reassessment-fallback", 300, reason=REASSESSMENT_DEFERRED_REASON)
            await _success(db, "normal-renewal", 550, attempt_index=0, reason="request_success")
            assert not (await _preference(db, 599)).reassessment_due
            assert (await _preference(db, 600)).reassessment_due
            events = (await db.scalars(select(SessionLeaseEvent).order_by(SessionLeaseEvent.id))).all()
            assert [(item.event_type, item.reason) for item in events] == [
                ("assigned", "first_success"),
                ("renewed", REASSESSMENT_DEFERRED_REASON),
            ]
    asyncio.run(exercise())


def test_filtered_unavailable_lease_can_reassess_an_attempt_zero_migration():
    async def exercise():
        async with _database() as (db, _):
            await _success(
                db, "native-owner", 0, channel_id=1, attempt_index=0,
                provider_protocol="openai", reason="request_success",
            )
            await _success(
                db, "cooldown-migration", 30, attempt_index=0,
                reason="leased_channel_unavailable",
            )
            assert not (await _preference(db, 329)).reassessment_due
            assert (await _preference(db, 330)).reassessment_due
    asyncio.run(exercise())


def test_verified_reassessment_checkpoint_allows_an_attempt_zero_success():
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "first-fallback", 0)
            await _success(
                db, "reassessment-fallback", 300, attempt_index=0,
                reason=REASSESSMENT_DEFERRED_REASON,
            )
            assert (await _preference(db, 600)).reassessment_due
    asyncio.run(exercise())


@pytest.mark.parametrize("overrides", [
    {"active_native_channel_ids": set()},
    {"reassess_seconds": 0},
    {"channels": [SimpleNamespace(id=2, protocol="openai")]},
    {"active_native_channel_ids": {1, 2}},
    {"channels": []},
])
def test_unneeded_reassessment_does_not_query_event_history(overrides):
    async def exercise():
        async with _database() as (db, engine):
            await _success(db, "first-fallback", 0)
            statements = []

            @event.listens_for(engine.sync_engine, "before_cursor_execute")
            def track_sql(_connection, _cursor, statement, _params, _context, _many):
                statements.append(statement)

            preference = await _preference(db, 300, **overrides)
            assert preference.channel_id == 2
            assert not preference.reassessment_due
            assert len(statements) == 1
            assert "session_lease_events" not in statements[0]
            assert "request_attempts" not in statements[0]
            assert not db.new and not db.dirty
    asyncio.run(exercise())


@pytest.mark.parametrize("attempt_fields", [
    {"attempt_index": 0},
    {"provider_protocol": "openai"},
    {"provider_protocol": None},
    {"request_protocol": "openai_responses"},
])
def test_unproven_cross_protocol_fallback_keeps_existing_lease(attempt_fields):
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "initial-success", 0, **attempt_fields)
            preference = await _preference(db, 300)
            assert preference.channel_id == 2
            assert not preference.reassessment_due
    asyncio.run(exercise())


@pytest.mark.parametrize("latest_type", ["assigned", "migrated", "renewed"])
def test_latest_event_missing_attempt_evidence_never_reuses_an_older_fallback(latest_type):
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "older-proven-fallback", 0)
            lease = (await db.scalars(select(SessionLease))).one()
            db.add(SessionLeaseEvent(
                lease_id=lease.id,
                request_id="latest-missing-attempt",
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                event_type=latest_type,
                previous_channel_id=1,
                channel_id=2,
                reason=REASSESSMENT_DEFERRED_REASON if latest_type == "renewed" else "fallback_success",
                created_at=START + timedelta(seconds=50),
            ))
            await db.commit()
            assert not (await _preference(db, 350)).reassessment_due
    asyncio.run(exercise())


def test_late_deferred_success_cannot_take_back_a_recovered_lease():
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "first-fallback", 0)
            await _success(
                db, "recovered", 300, channel_id=1, attempt_index=0,
                provider_protocol="openai", reason="protocol_recovered",
            )
            result = await _success(db, "late-old-fallback", 320, reason=REASSESSMENT_DEFERRED_REASON)
            lease = (await db.scalars(select(SessionLease))).one()
            assert result.event_types == ()
            assert result.channel_id == lease.channel_id == 1
            assert lease.last_used_at.replace(tzinfo=timezone.utc) == START + timedelta(seconds=300)
            events = (await db.scalars(select(SessionLeaseEvent).order_by(SessionLeaseEvent.id))).all()
            assert [item.reason for item in events] == ["first_success", "protocol_recovered"]
    asyncio.run(exercise())


def test_deferred_success_refreshes_a_cached_lease_after_another_session_recovers():
    async def exercise():
        async with _database() as (old_db, engine):
            await _success(old_db, "first-fallback", 0)
            cached_lease = (await old_db.scalars(select(SessionLease))).one()
            async with async_sessionmaker(engine, expire_on_commit=False)() as new_db:
                await _success(
                    new_db, "recovered-elsewhere", 300, channel_id=1,
                    attempt_index=0, provider_protocol="openai", reason="protocol_recovered",
                )
            assert cached_lease.channel_id == 2
            result = await _success(
                old_db, "late-cached-fallback", 320, reason=REASSESSMENT_DEFERRED_REASON,
            )
            assert result.event_types == ()
            async with async_sessionmaker(engine, expire_on_commit=False)() as check_db:
                lease = (await check_db.scalars(select(SessionLease))).one()
                assert lease.channel_id == 1
                assert lease.last_used_at.replace(tzinfo=timezone.utc) == START + timedelta(seconds=300)
                events = (await check_db.scalars(select(SessionLeaseEvent))).all()
                assert [item.reason for item in events] == ["first_success", "protocol_recovered"]
    asyncio.run(exercise())


def test_deferred_success_does_not_create_a_missing_lease():
    async def exercise():
        async with _database() as (db, _):
            result = await _success(db, "late-fallback", 300, reason=REASSESSMENT_DEFERRED_REASON)
            assert result.event_types == ()
            assert (await db.scalars(select(SessionLease))).all() == []
            assert (await db.scalars(select(SessionLeaseEvent))).all() == []
    asyncio.run(exercise())


def test_expired_or_absent_session_has_no_preferred_channel():
    async def exercise():
        async with _database() as (db, _):
            await _success(db, "first-fallback", 0)
            assert (await _preference(db, 1800)).channel_id is None
            assert (await _preference(db, 300, session_id=None)).channel_id is None
    asyncio.run(exercise())
