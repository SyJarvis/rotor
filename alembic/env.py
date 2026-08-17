from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from alembic import context
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rotor.config import settings
from rotor.database import Base
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.models.log import RequestLog
from rotor.models.conversation import ConversationRecord
from rotor.models.usage import UsageLedger
from rotor.models.response_route import ResponseRoute
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.request_attempt import RequestAttempt
from rotor.models.mcp_control_key import MCPControlKey

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

def _sync_database_url(url: str) -> str:
    """Return a synchronous SQLAlchemy URL for Alembic migrations."""
    parsed = make_url(url)
    drivername = parsed.drivername
    if drivername == "sqlite+aiosqlite":
        parsed = parsed.set(drivername="sqlite")
    elif drivername == "postgresql+asyncpg":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


# Set the SQLAlchemy URL from settings. The app uses async drivers at runtime,
# while Alembic runs migrations through SQLAlchemy's synchronous engine.
config.set_main_option("sqlalchemy.url", _sync_database_url(settings.DATABASE_URL))

# add your model's MetaData object here
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
