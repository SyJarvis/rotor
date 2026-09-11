import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.database import Base
from rotor.gateway.accounting import AccountingService, UsageData
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


class AccountingTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'accounting.db'}",
            pool_size=100,
            max_overflow=0,
            connect_args={"timeout": 30},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            token = Token(key="accounting-test", name="test", quota=500)
            self.channel = Channel(
                name="test", type="openai", protocol="openai",
                key="unused", base_url="https://unused.invalid", extra={},
            )
            db.add_all([token, self.channel])
            await db.commit()
            self.token_id = token.id
        self.service = AccountingService()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.directory.cleanup()

    async def record(self, db, token, request_id, amount=11):
        await self.service.record_success(
            db, request_id=request_id, conversation_id=None,
            request_protocol="openai_chat", token=token, channel=self.channel,
            model="test", provider_model="test",
            usage=UsageData(total_tokens=amount), latency_ms=1,
            client_ip="127.0.0.1",
        )

    async def test_100_concurrent_stale_reads_match_ledger_and_disable_quota(self):
        ready = asyncio.Event()
        loaded = 0

        async def request(index):
            nonlocal loaded
            async with self.sessions() as db:
                token = await db.get(Token, self.token_id)
                self.assertEqual(token.used_quota, 0)
                loaded += 1
                if loaded == 100:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=30)
                await self.record(db, token, f"request-{index}")
                await db.commit()

        await asyncio.gather(*(request(index) for index in range(100)))
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            count, total = (await db.execute(select(
                func.count(UsageLedger.id), func.sum(UsageLedger.total_tokens)
            ))).one()
            self.assertEqual((count, total), (100, 1100))
            self.assertEqual(token.request_count, count)
            self.assertEqual(token.token_count, total)
            self.assertEqual(token.used_quota, total)
            self.assertFalse(token.enabled)

    async def test_rollback_reverts_counters_and_ledger_together(self):
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            await self.record(db, token, "rolled-back", amount=600)
            await db.flush()
            await db.rollback()
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            self.assertEqual((token.request_count, token.token_count, token.used_quota), (0, 0, 0))
            self.assertTrue(token.enabled)
            self.assertEqual(await db.scalar(select(func.count()).select_from(UsageLedger)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(RequestLog)), 0)

    async def test_stale_request_uses_current_quota_and_preserves_disabled_state(self):
        async with self.sessions() as stale, self.sessions() as admin:
            token = await stale.get(Token, self.token_id)
            await admin.execute(update(Token).where(Token.id == self.token_id).values(quota=10))
            await admin.commit()
            await self.record(stale, token, "quota-changed")
            await stale.commit()
        async with self.sessions() as db:
            self.assertFalse((await db.get(Token, self.token_id)).enabled)
        async with self.sessions() as stale, self.sessions() as admin:
            token = await stale.get(Token, self.token_id)
            await admin.execute(update(Token).where(Token.id == self.token_id).values(quota=None))
            await admin.commit()
            await self.record(stale, token, "disabled")
            token.name = "unrelated edit"
            await stale.commit()
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            self.assertFalse(token.enabled)
            self.assertEqual((token.request_count, token.used_quota), (2, 22))

    async def test_last_used_timestamp_is_throttled_using_current_row(self):
        now = datetime.now(timezone.utc)
        async with self.sessions() as db:
            await db.execute(update(Token).values(last_used_at=now))
            await db.commit()
            token = await db.get(Token, self.token_id)
            await self.record(db, token, "recent")
            await db.commit()
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            self.assertEqual(token.last_used_at.replace(tzinfo=timezone.utc), now)
            token.last_used_at = now - timedelta(minutes=2)
            await db.commit()
            await self.record(db, token, "stale")
            await db.commit()
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            self.assertGreaterEqual(token.last_used_at.replace(tzinfo=timezone.utc), now)

    async def test_request_started_before_admin_disable_does_not_reenable_token(self):
        async with self.sessions() as stale, self.sessions() as admin:
            token = await stale.get(Token, self.token_id)
            self.assertTrue(token.enabled)
            await admin.execute(
                update(Token).where(Token.id == self.token_id).values(enabled=False)
            )
            await admin.commit()
            await self.record(stale, token, "disabled-in-flight")
            token.name = "unrelated edit"
            await stale.commit()
        async with self.sessions() as db:
            token = await db.get(Token, self.token_id)
            self.assertFalse(token.enabled)
            self.assertEqual(token.used_quota, 11)
