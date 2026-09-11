import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotor.database as database
from rotor.models.token import Token


class DatabaseSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'session.db'}"
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Token.__table__.create)
        self.factory_patch = patch.object(database, "async_session_maker", self.sessions)
        self.factory_patch.start()

    async def asyncTearDown(self):
        self.factory_patch.stop()
        await self.engine.dispose()
        self.directory.cleanup()

    async def finish(self, dependency):
        with self.assertRaises(StopAsyncIteration):
            await anext(dependency)

    async def names(self):
        async with self.sessions() as db:
            return list((await db.scalars(select(Token.name))).all())

    async def test_normal_exit_commits_flushed_orm_insert(self):
        dependency = database.get_db()
        db = await anext(dependency)
        db.add(Token(key="flushed", name="flushed"))
        await db.flush()
        self.assertFalse(db.new or db.dirty or db.deleted)
        await self.finish(dependency)
        self.assertEqual(await self.names(), ["flushed"])
        self.assertEqual(self.engine.pool.checkedout(), 0)

    async def test_normal_exit_commits_core_update(self):
        async with self.sessions() as seed:
            seed.add(Token(key="core", name="before"))
            await seed.commit()
        dependency = database.get_db()
        db = await anext(dependency)
        await db.execute(update(Token).values(name="after"))
        self.assertFalse(db.new or db.dirty or db.deleted)
        await self.finish(dependency)
        self.assertEqual(await self.names(), ["after"])

    async def test_explicit_commit_is_not_committed_again(self):
        dependency = database.get_db()
        db = await anext(dependency)
        commits = []
        event.listen(db.sync_session, "after_commit", lambda session: commits.append(True))
        db.add(Token(key="explicit", name="explicit"))
        await db.commit()
        await self.finish(dependency)
        self.assertEqual(commits, [True])
        self.assertEqual(await self.names(), ["explicit"])

    async def test_exception_rolls_back_flushed_insert_and_releases_connection(self):
        for error in (RuntimeError("handler failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                dependency = database.get_db()
                db = await anext(dependency)
                db.add(Token(key="rollback", name="rollback"))
                await db.flush()
                with self.assertRaises(type(error)):
                    await dependency.athrow(error)
                self.assertEqual(await self.names(), [])
                self.assertEqual(self.engine.pool.checkedout(), 0)

    async def test_cancellation_during_commit_finishes_commit_and_closes_session(self):
        dependency = database.get_db()
        db = await anext(dependency)
        db.add(Token(key="cancel-commit", name="committed"))
        started = asyncio.Event()
        release = asyncio.Event()
        committed = asyncio.Event()
        real_commit = db.commit

        async def paused_commit():
            started.set()
            await release.wait()
            await real_commit()
            committed.set()

        with patch.object(db, "commit", paused_commit):
            cleanup = asyncio.create_task(anext(dependency))
            await asyncio.wait_for(started.wait(), timeout=5)
            cleanup.cancel()
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup
        self.assertTrue(committed.is_set())
        self.assertEqual(await self.names(), ["committed"])
        self.assertEqual(self.engine.pool.checkedout(), 0)

    async def test_engine_factory_applies_production_sqlite_settings_per_connection(self):
        engine = database.create_database_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'factory.db'}"
        )
        try:
            async with engine.connect() as first, engine.connect() as second:
                for connection in (first, second):
                    self.assertEqual(await connection.scalar(text("PRAGMA journal_mode")), "wal")
                    self.assertEqual(await connection.scalar(text("PRAGMA busy_timeout")), 5000)
                    self.assertEqual(await connection.scalar(text("PRAGMA synchronous")), 1)
        finally:
            await engine.dispose()
