"""Run and validate Rotor's packaged SQLite migrations at startup."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import sqlite3

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, make_url

from rotor.database import Base
from rotor.models.admin_auth import (
    AdminAuthEvent,
    AdminLoginThrottle,
    AdminSession,
    AdminUser,
)
from rotor.models.channel import Channel
from rotor.models.conversation import ConversationRecord
from rotor.models.log import RequestLog
from rotor.models.mcp_control_key import MCPControlKey
from rotor.models.request_attempt import RequestAttempt
from rotor.models.response_route import ResponseRoute
from rotor.models.routing_decision import RoutingDecisionRecord
from rotor.models.session_lease import SessionLease, SessionLeaseEvent
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


logger = logging.getLogger(__name__)

# Referencing the model types keeps their imports explicit: importing this module
# must register the complete application schema before it compares metadata.
_REGISTERED_MODELS = (
    AdminAuthEvent,
    AdminLoginThrottle,
    AdminSession,
    AdminUser,
    Channel,
    ConversationRecord,
    MCPControlKey,
    RequestAttempt,
    RequestLog,
    ResponseRoute,
    RoutingDecisionRecord,
    SessionLease,
    SessionLeaseEvent,
    Token,
    UsageLedger,
)

_PRE_ADMIN_REVISION = "f6a7b8c9d0e1"
_ADMIN_TABLES = frozenset(
    {
        "admin_auth_events",
        "admin_login_throttles",
        "admin_sessions",
        "admin_users",
    }
)


class LegacyDatabaseError(RuntimeError):
    """Raised when an unversioned database cannot be adopted safely."""


def _sqlite_url_and_path(database_url: str) -> tuple[str, Path]:
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError("Rotor startup migrations currently require SQLite")
    if parsed.database in (None, "", ":memory:"):
        raise ValueError(
            "Rotor startup migrations require a file-backed SQLite database"
        )

    path = Path(parsed.database).expanduser().resolve()
    sync_url = parsed.set(drivername="sqlite", database=str(path))
    return sync_url.render_as_string(hide_password=False), path


def _alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "rotor:migrations")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _is_admin_addition(difference: object) -> bool:
    if not isinstance(difference, tuple) or len(difference) < 2:
        return False

    operation, schema_object = difference[:2]
    if operation == "add_table":
        return getattr(schema_object, "name", None) in _ADMIN_TABLES
    if operation == "add_index":
        table = getattr(schema_object, "table", None)
        return getattr(table, "name", None) in _ADMIN_TABLES
    return False


def _legacy_revision(connection: Connection) -> str:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True},
    )
    differences = compare_metadata(context, Base.metadata)
    if not differences:
        return "head"
    if all(_is_admin_addition(difference) for difference in differences):
        return _PRE_ADMIN_REVISION
    raise LegacyDatabaseError(
        "cannot safely adopt unversioned SQLite database: "
        "its schema does not match a known Rotor release"
    )


def _backup_database(database_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(
        f"{database_path.name}.pre-alembic-{timestamp}.bak"
    )
    temporary_path = backup_path.with_suffix(f"{backup_path.suffix}.tmp")

    try:
        with (
            sqlite3.connect(
                f"{database_path.as_uri()}?mode=ro",
                uri=True,
            ) as source,
            sqlite3.connect(temporary_path) as destination,
        ):
            source.backup(destination)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(backup_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return backup_path


def run_startup_migrations(database_url: str) -> Path | None:
    """Upgrade a SQLite database to head, adopting known legacy schemas.

    Unversioned databases are stamped only when their reflected schema exactly
    matches the current models or the last release before administrator tables.
    A consistent SQLite backup is created before that first Alembic write.
    """
    sync_url, database_path = _sqlite_url_and_path(database_url)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = _alembic_config(sync_url)
    backup_path: Path | None = None
    legacy_revision: str | None = None

    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            current_heads = MigrationContext.configure(
                connection
            ).get_current_heads()
            application_tables = table_names - {"alembic_version"}
            if application_tables and not current_heads:
                legacy_revision = _legacy_revision(connection)
    finally:
        engine.dispose()

    if legacy_revision is not None:
        backup_path = _backup_database(database_path)
        logger.warning(
            "Adopting unversioned SQLite database at revision %s; backup: %s",
            legacy_revision,
            backup_path,
        )
        command.stamp(config, legacy_revision)

    command.upgrade(config, "head")
    command.check(config)
    return backup_path
