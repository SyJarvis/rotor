import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.database import Base
from rotor.models.channel import Channel  # noqa: F401
from rotor.models.session_lease import SessionLease, SessionLeaseEvent
from rotor.models.token import Token  # noqa: F401
from rotor.services.session_leases import (
    get_preferred_channel_id,
    record_session_lease_success,
    resolve_idle_ttl_seconds,
)


def test_session_lease_assigns_renews_migrates_and_expires() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        started_at = datetime(2026, 8, 19, 2, 0, tzinfo=timezone.utc)
        async with sessions() as db:
            assigned = await record_session_lease_success(
                db,
                request_id="req-1",
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                channel_id=11,
                idle_ttl_seconds=900,
                now=started_at,
            )
            await db.commit()
            assert assigned.event_types == ("assigned",)
            assert await get_preferred_channel_id(
                db,
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                now=started_at + timedelta(minutes=1),
            ) == 11

            renewed = await record_session_lease_success(
                db,
                request_id="req-2",
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                channel_id=11,
                idle_ttl_seconds=900,
                now=started_at + timedelta(minutes=1),
            )
            migrated = await record_session_lease_success(
                db,
                request_id="req-3",
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                channel_id=12,
                idle_ttl_seconds=900,
                migration_reason="fallback_success",
                now=started_at + timedelta(minutes=2),
            )
            await db.commit()

            assert renewed.event_types == ("renewed",)
            assert migrated.event_types == ("migrated",)
            assert migrated.previous_channel_id == 11
            assert await get_preferred_channel_id(
                db,
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                now=started_at + timedelta(minutes=10),
            ) == 12
            assert await get_preferred_channel_id(
                db,
                token_id=8,
                session_id="session-a",
                logical_model="model-a",
                now=started_at + timedelta(minutes=10),
            ) is None
            assert await get_preferred_channel_id(
                db,
                token_id=7,
                session_id="session-a",
                logical_model="model-b",
                now=started_at + timedelta(minutes=10),
            ) is None

            assert await get_preferred_channel_id(
                db,
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                now=started_at + timedelta(minutes=18),
            ) is None
            reassigned = await record_session_lease_success(
                db,
                request_id="req-4",
                token_id=7,
                session_id="session-a",
                logical_model="model-a",
                channel_id=11,
                idle_ttl_seconds=900,
                now=started_at + timedelta(minutes=18),
            )
            await db.commit()

            assert reassigned.event_types == ("expired", "assigned")
            lease = (
                await db.scalars(select(SessionLease))
            ).one()
            assert lease.channel_id == 11
            events = list(
                (
                    await db.scalars(
                        select(SessionLeaseEvent).order_by(SessionLeaseEvent.id)
                    )
                ).all()
            )
            # Same-channel hits refresh the lease without a renewal event.
            assert [event.event_type for event in events] == [
                "assigned",
                "migrated",
                "expired",
                "assigned",
            ]
            assert [event.reason for event in events] == [
                "first_success",
                "fallback_success",
                "idle_timeout",
                "post_expiry_success",
            ]

        await engine.dispose()

    asyncio.run(exercise())


def test_same_channel_success_persists_sliding_idle_expiry(tmp_path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'renewal.db'}"
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        started_at = datetime(2026, 8, 19, 2, 0, tzinfo=timezone.utc)
        renewed_at = started_at + timedelta(minutes=14)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            async with sessions() as db:
                await record_session_lease_success(
                    db,
                    request_id="req-assigned",
                    token_id=7,
                    session_id="session-active",
                    logical_model="model-a",
                    channel_id=11,
                    idle_ttl_seconds=900,
                    now=started_at,
                )
                await db.commit()

            async with sessions() as db:
                renewed = await record_session_lease_success(
                    db,
                    request_id="req-renewed",
                    token_id=7,
                    session_id="session-active",
                    logical_model="model-a",
                    channel_id=11,
                    idle_ttl_seconds=900,
                    now=renewed_at,
                )
                await db.commit()
                assert renewed.event_types == ("renewed",)
                assert renewed.previous_channel_id == renewed.channel_id == 11

            async with sessions() as db:
                assert await get_preferred_channel_id(
                    db,
                    token_id=7,
                    session_id="session-active",
                    logical_model="model-a",
                    now=started_at + timedelta(minutes=16),
                ) == 11
                lease = (await db.scalars(select(SessionLease))).one()
                assert lease.last_used_at.replace(tzinfo=timezone.utc) == renewed_at
                assert lease.expires_at.replace(tzinfo=timezone.utc) == (
                    renewed_at + timedelta(minutes=15)
                )
                assert await get_preferred_channel_id(
                    db,
                    token_id=7,
                    session_id="session-active",
                    logical_model="model-a",
                    now=started_at + timedelta(minutes=30),
                ) is None
                events = (await db.scalars(select(SessionLeaseEvent))).all()
                assert [event.event_type for event in events] == ["assigned"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_session_lease_ttl_supports_model_then_channel_override() -> None:
    channel = SimpleNamespace(
        extra={
            "session_lease_idle_ttl_seconds": 600,
            "session_lease_idle_ttl_by_model": {
                "model-a": 1_200,
                "*": 300,
            },
        }
    )

    assert resolve_idle_ttl_seconds(channel, "model-a", 900) == 1_200
    assert resolve_idle_ttl_seconds(channel, "model-b", 900) == 300


def test_session_lease_ttl_invalid_override_uses_safe_default() -> None:
    channel = SimpleNamespace(
        extra={"session_lease_idle_ttl_seconds": 30}
    )

    assert resolve_idle_ttl_seconds(channel, "model-a", 900) == 900


def test_concurrent_first_assignment_creates_one_authoritative_lease(
    tmp_path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "session-leases.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            connect_args={"timeout": 5},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async def assign(request_id: str) -> None:
            async with sessions() as db:
                await record_session_lease_success(
                    db,
                    request_id=request_id,
                    token_id=7,
                    session_id="session-concurrent",
                    logical_model="model-a",
                    channel_id=11,
                    idle_ttl_seconds=900,
                )
                await db.commit()

        await asyncio.gather(assign("req-a"), assign("req-b"))

        async with sessions() as db:
            lease_count = await db.scalar(
                select(func.count()).select_from(SessionLease)
            )
            events = list(
                (
                    await db.scalars(
                        select(SessionLeaseEvent).order_by(SessionLeaseEvent.id)
                    )
                ).all()
            )
        await engine.dispose()

        assert lease_count == 1
        # The loser of the first-assignment race renews the lease without
        # persisting a second event row.
        assert [item.event_type for item in events] == [
            "assigned",
        ]

    asyncio.run(exercise())
