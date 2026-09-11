import asyncio
import logging
import time

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.engine import make_url
from sqlalchemy.pool import AsyncAdaptedQueuePool
from rotor.config import settings
import rotor.observability as observability


logger = logging.getLogger(__name__)


class _MeasuredQueuePool(AsyncAdaptedQueuePool):
    def connect(self):
        started = time.perf_counter()
        try:
            return super().connect()
        except BaseException:
            observability.performance_metrics.observe("db.acquire_errors", 1)
            raise
        finally:
            observability.performance_metrics.observe("db.acquire_ms", (time.perf_counter() - started) * 1000)


def _instrument_engine(db_engine):
    sync_engine = db_engine.sync_engine

    @event.listens_for(sync_engine, "before_cursor_execute")
    def before_sql(conn, cursor, statement, parameters, context, executemany):
        context._rotor_started = None if observability.metrics_suppressed() else time.perf_counter()

    def finish_sql(context, failed=False):
        started = getattr(context, "_rotor_started", None)
        if started is not None:
            context._rotor_started = None
            observability.performance_metrics.observe("db.sql_ms", (time.perf_counter() - started) * 1000)
            if failed:
                observability.performance_metrics.observe("db.sql_errors", 1)

    @event.listens_for(sync_engine, "after_cursor_execute")
    def after_sql(conn, cursor, statement, parameters, context, executemany):
        finish_sql(context)

    @event.listens_for(sync_engine, "handle_error")
    def sql_error(exception_context):
        finish_sql(exception_context.execution_context, failed=True)

    @event.listens_for(sync_engine, "checkout")
    def checkout(connection, record, proxy):
        record.info["rotor_checkout"] = None if observability.metrics_suppressed() else time.perf_counter()

    @event.listens_for(sync_engine, "checkin")
    def checkin(connection, record):
        started = record.info.pop("rotor_checkout", None)
        if started is not None:
            observability.performance_metrics.observe("db.hold_ms", (time.perf_counter() - started) * 1000)

    original_commit = sync_engine.dialect.do_commit

    def commit(connection):
        started = time.perf_counter()
        try:
            return original_commit(connection)
        except BaseException:
            observability.performance_metrics.observe("db.commit_errors", 1)
            raise
        finally:
            observability.performance_metrics.observe("db.commit_ms", (time.perf_counter() - started) * 1000)

    sync_engine.dialect.do_commit = commit


def create_database_engine(database_url: str, *, echo: bool = False):
    """Create an engine with the gateway's connection settings."""
    url = make_url(database_url)
    default_pool = url.get_dialect(_is_async=True).get_pool_class(url)
    pool_options = {"poolclass": _MeasuredQueuePool} if issubclass(default_pool, AsyncAdaptedQueuePool) else {}
    db_engine = create_async_engine(database_url, echo=echo, future=True, **pool_options)
    # WAL allows reads alongside writes; busy_timeout lets writers queue.
    if database_url.startswith("sqlite"):
        @event.listens_for(db_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
    _instrument_engine(db_engine)
    return db_engine


engine = create_database_engine(
    settings.DATABASE_URL,
    echo=settings.LOG_LEVEL == "DEBUG",
)

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
        # Flushed ORM changes and Core DML can leave the dirty collections
        # empty while the transaction still needs committing.
        if session.in_transaction():
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
