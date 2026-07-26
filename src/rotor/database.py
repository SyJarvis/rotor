import asyncio
import logging

from sqlalchemy import event
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
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
