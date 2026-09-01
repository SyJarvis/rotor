import asyncio
import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import DeclarativeBase
from rotor.config import settings


logger = logging.getLogger(__name__)

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.LOG_LEVEL == "DEBUG",
    future=True
)

# SQLite under default PRAGMAs (journal_mode=DELETE, busy_timeout=0) throws
# "database is locked" the instant two sessions write concurrently. The
# conversation-store worker, the request-path accounting session, and the
# streaming generator's short-lived session all write to the same file, so
# lock collisions are frequent under load. WAL lets reads proceed while a
# write is in flight, busy_timeout makes writers queue instead of failing
# immediately, and synchronous=NORMAL is the safe+fast pairing for WAL.
if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def _cancellation_safe(awaitable) -> None:
    """Finish a DB operation before propagating task cancellation."""
    task = asyncio.create_task(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The request task was cancelled (commonly after an SSE client exits),
        # but shield keeps the DB operation alive. Wait for it to finish so an
        # aiosqlite connection is never returned to the pool half-closed.
        await task
        raise


async def get_db() -> AsyncSession:
    """Yield a request session with cancellation-safe transaction cleanup."""
    session = async_session_maker()
    try:
        yield session
        await _cancellation_safe(session.commit())
    except BaseException:
        if session.in_transaction():
            try:
                await _cancellation_safe(session.rollback())
            except Exception:
                logger.exception("Failed to roll back request database session")
        raise
    finally:
        try:
            await _cancellation_safe(session.close())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to close request database session")


async def init_db():
    """Migrate the database and ensure the default administrator exists."""
    from rotor.migrations.runner import run_startup_migrations

    await asyncio.to_thread(run_startup_migrations, settings.DATABASE_URL)

    from rotor.core.admin_auth import ensure_default_admin

    async with async_session_maker() as session:
        admin = await ensure_default_admin(session)
        if admin.must_change_password:
            logger.warning(
                "Default administrator password must be changed before "
                "the management API can be used"
            )


_USAGE_FACT_COLUMNS = {
    "uncached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_5m_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0",
    "usage_schema_version": "VARCHAR(10) NOT NULL DEFAULT '1'",
    "capacity_snapshot": "JSON",
    "cache_scope": "VARCHAR(100)",
    "capacity_scope": "VARCHAR(100)",
    "billing_scope": "VARCHAR(100)",
    "currency": "VARCHAR(10) NOT NULL DEFAULT 'USD'",
    "cost_status": "VARCHAR(30) NOT NULL DEFAULT 'unknown'",
    "tariff_version": "VARCHAR(100)",
    "tariff_period": "VARCHAR(100)",
    "tariff_snapshot": "JSON",
}


def _ensure_usage_fact_columns(connection) -> None:
    """Add phase-2 usage columns to databases created before this release."""
    schema = inspect(connection)
    for table_name in ("usage_ledger", "request_logs"):
        if not schema.has_table(table_name):
            continue
        existing = {
            column["name"] for column in schema.get_columns(table_name)
        }
        for column_name, definition in _USAGE_FACT_COLUMNS.items():
            if column_name in existing:
                continue
            connection.execute(text(
                f'ALTER TABLE "{table_name}" ADD COLUMN '
                f'"{column_name}" {definition}'
            ))
