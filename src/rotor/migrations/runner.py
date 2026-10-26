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
from sqlalchemy import TEXT, Integer, String, Text, create_engine, inspect
from sqlalchemy.engine import Connection, make_url

from rotor.database import Base
from rotor.models.admin_auth import (
    AdminAuthEvent,
    AdminLoginThrottle,
    AdminSession,
    AdminUser,
    UserInvitation,
)
from rotor.models.channel import Channel
from rotor.models.channel_oauth import ChannelOAuthSession
from rotor.models.client_oauth import OAuthAuthorizationCode, OAuthCredential, OAuthGrant
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
    UserInvitation,
    Channel,
    ChannelOAuthSession,
    OAuthAuthorizationCode,
    OAuthCredential,
    OAuthGrant,
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

_PRE_USER_REVISION = "e1f2a3b4c5d6"
_PRE_CLIENT_OAUTH_REVISION = "f2a3b4c5d6e7"
_CLIENT_OAUTH_TABLES = frozenset({"client_oauth_codes", "client_oauth_grants", "client_oauth_credentials"})
_CLIENT_OAUTH_BINDING_TABLES = frozenset({"client_oauth_codes", "client_oauth_grants"})
_PRE_ACCOUNT_OAUTH_REVISION = "b4c5d6e7f8a9"
_PRE_ACCOUNT_REVISION = "a3b4c5d6e7f8"
_ACCOUNT_SCOPE_TABLES = frozenset({"response_routes", "session_leases", "session_lease_events"})
_ACCOUNT_SCOPE_UNIQUES = {
    "response_routes": ("uq_response_routes_owner_response", ["owner_id", "response_id"]),
    "session_leases": ("uq_session_leases_owner_session_model", ["owner_id", "session_id", "logical_model"]),
}
_ACCOUNT_SCOPE_CHECK = "(token_id IS NOT NULL AND owner_id IS NULL) OR (token_id IS NULL AND owner_id IS NOT NULL)"
_USER_COLUMNS = {
    "admin_users": {"role", "display_name", "quota", "used_quota", "allowed_channels", "allowed_models", "default_token_id"},
    "tokens": {"owner_id", "is_base", "allowed_models"},
    "request_logs": {"owner_id"}, "usage_ledger": {"owner_id"}, "conversation_records": {"owner_id"},
}
_USER_INDEXES = {"ix_tokens_owner_id", "uq_tokens_owner_base", "ix_request_logs_owner_id", "ix_usage_ledger_owner_id", "ix_conversation_records_owner_id"}


def _is_user_addition(difference):
    operation = difference[0]
    if operation == "add_table":
        return difference[1].name == "user_invitations"
    if operation == "add_column" and len(difference) == 4:
        return difference[3].name in _USER_COLUMNS.get(difference[2], set())
    if operation == "add_index":
        return difference[1].name in _USER_INDEXES
    if operation == "add_fk":
        constraint = difference[1]
        return (constraint.table.name == "tokens" and list(constraint.column_keys) == ["owner_id"]
                and [element.target_fullname for element in constraint.elements] == ["admin_users.id"])
    return False


def _validate_user_schema_generation(connection):
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    observed = [(table, column) for table, columns in _USER_COLUMNS.items() if table in tables
                for column in columns if column in {item["name"] for item in schema.get_columns(table)}]
    expected = [(table, column) for table, columns in _USER_COLUMNS.items() for column in columns]
    if (observed or "user_invitations" in tables) and (set(observed) != set(expected) or "user_invitations" not in tables):
        raise LegacyDatabaseError("cannot safely adopt unversioned SQLite database: partial user ownership schema")


def _validate_account_scope_generation(connection):
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    columns = {table: {column["name"]: column for column in schema.get_columns(table)}
               for table in _ACCOUNT_SCOPE_TABLES if table in tables}
    observed = {table for table, fields in columns.items() if "owner_id" in fields}
    if not observed:
        if any(fields.get("token_id", {}).get("nullable") for fields in columns.values()):
            raise LegacyDatabaseError("cannot safely adopt SQLite database: partial account identity schema")
        return False
    expected_check = "".join(_ACCOUNT_SCOPE_CHECK.split()).lower()
    for table in _ACCOUNT_SCOPE_TABLES:
        if (table not in observed or not columns[table]["owner_id"]["nullable"]
                or not columns[table].get("token_id", {}).get("nullable")
                or not isinstance(columns[table]["owner_id"]["type"], Integer)
                or not isinstance(columns[table]["token_id"]["type"], Integer)):
            raise LegacyDatabaseError("cannot safely adopt SQLite database: partial account identity schema")
        owner_fks = [fk for fk in schema.get_foreign_keys(table) if "owner_id" in fk["constrained_columns"]]
        if table == "session_lease_events":
            valid_fk = not owner_fks
        else:
            valid_fk = len(owner_fks) == 1 and (
                owner_fks[0]["constrained_columns"] == ["owner_id"]
                and owner_fks[0]["referred_table"] == "admin_users"
                and owner_fks[0]["referred_columns"] == ["id"]
                and owner_fks[0].get("options", {}).get("ondelete", "").upper() == "CASCADE")
        if not valid_fk:
            raise LegacyDatabaseError("cannot safely adopt SQLite database: partial account identity foreign keys")
        checks = {check["name"]: "".join(check["sqltext"].split()).lower() for check in schema.get_check_constraints(table)}
        indexes = {index["name"]: index["column_names"] for index in schema.get_indexes(table)}
        if (checks.get(f"ck_{table}_identity") != expected_check
                or indexes.get(f"ix_{table}_owner_id") != ["owner_id"]):
            raise LegacyDatabaseError("cannot safely adopt SQLite database: partial account identity constraints")
        if table in _ACCOUNT_SCOPE_UNIQUES:
            name, fields = _ACCOUNT_SCOPE_UNIQUES[table]
            uniques = {constraint["name"]: constraint["column_names"] for constraint in schema.get_unique_constraints(table)}
            if uniques.get(name) != fields:
                raise LegacyDatabaseError("cannot safely adopt SQLite database: partial account identity constraints")
    return True


def _validate_client_oauth_binding_generation(connection):
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    observed_tables = _CLIENT_OAUTH_TABLES & tables
    if not observed_tables:
        return False
    if observed_tables != _CLIENT_OAUTH_TABLES:
        raise LegacyDatabaseError("cannot safely adopt SQLite database: partial client OAuth schema")
    columns = {table: {column["name"]: column for column in schema.get_columns(table)}
               for table in _CLIENT_OAUTH_BINDING_TABLES}
    observed = {table for table, fields in columns.items() if "token_id" in fields}
    if not observed:
        return False
    for table in _CLIENT_OAUTH_BINDING_TABLES:
        token_column = columns[table].get("token_id", {})
        token_fks = [fk for fk in schema.get_foreign_keys(table) if "token_id" in fk["constrained_columns"]]
        if (table not in observed or token_column["nullable"] or not isinstance(token_column["type"], Integer)
                or len(token_fks) != 1 or token_fks[0]["constrained_columns"] != ["token_id"]
                or token_fks[0]["referred_table"] != "tokens" or token_fks[0]["referred_columns"] != ["id"]
                or token_fks[0].get("options", {}).get("ondelete", "").upper() != "CASCADE"):
            raise LegacyDatabaseError("cannot safely adopt SQLite database: partial client OAuth Key binding schema")
    return True


def _is_client_oauth_binding_removal(difference):
    operation = difference[0]
    if operation == "remove_column" and len(difference) == 4:
        return difference[1] is None and difference[2] in _CLIENT_OAUTH_BINDING_TABLES and difference[3].name == "token_id"
    if operation == "remove_fk":
        constraint = difference[1]
        return (constraint.table.name in _CLIENT_OAUTH_BINDING_TABLES and list(constraint.column_keys) == ["token_id"]
                and [element.target_fullname for element in constraint.elements] == ["tokens.id"])
    return False


def _is_account_scope_addition(difference):
    operation = difference[0]
    if operation == "add_column" and len(difference) == 4:
        return difference[2] in _ACCOUNT_SCOPE_TABLES and difference[3].name == "owner_id"
    if operation == "modify_nullable" and len(difference) == 7:
        return difference[2] in _ACCOUNT_SCOPE_TABLES and difference[3] == "token_id" and difference[5:] == (False, True)
    if operation == "add_index":
        index = difference[1]
        return index.table.name in _ACCOUNT_SCOPE_TABLES and index.name == f"ix_{index.table.name}_owner_id"
    if operation == "add_constraint":
        constraint = difference[1]
        expected = _ACCOUNT_SCOPE_UNIQUES.get(constraint.table.name)
        return expected is not None and constraint.name == expected[0] and list(constraint.columns.keys()) == expected[1]
    if operation == "add_fk":
        constraint = difference[1]
        return (constraint.table.name in {"response_routes", "session_leases"} and list(constraint.column_keys) == ["owner_id"]
                and [element.target_fullname for element in constraint.elements] == ["admin_users.id"])
    return False


_PRE_ADMIN_REVISION = "f6a7b8c9d0e1"
_PRE_SCHEMA_PARITY_REVISION = "b8c9d0e1f2a3"
_PRE_OAUTH_REVISION = "c9d0e1f2a3b4"
_PRE_CALLBACK_REVISION = "d0e1f2a3b4c5"
_CALLBACK_SESSION_COLUMNS = frozenset({"oauth_state", "pkce_verifier"})
_OAUTH_CHANNEL_COLUMNS = frozenset({
    "auth_type", "oauth_access_token", "oauth_refresh_token", "oauth_expires_at",
    "oauth_account_id", "oauth_status", "oauth_error", "oauth_refresh_owner",
    "oauth_refresh_lock_until",
})
_ADMIN_TABLES = frozenset(
    {
        "admin_auth_events",
        "admin_login_throttles",
        "admin_sessions",
        "admin_users",
    }
)
_SCHEMA_PARITY_TYPE_CHANGES = frozenset(
    {
        ("request_logs", "usage_schema_version", 10),
        ("request_logs", "cost_status", 30),
        ("usage_ledger", "usage_schema_version", 10),
        ("usage_ledger", "cost_status", 30),
    }
)


class LegacyDatabaseError(RuntimeError):
    """Raised when an unversioned database cannot be adopted safely."""


class UnsupportedDatabaseError(RuntimeError):
    """Raised when DATABASE_URL cannot be used by the startup migration path."""


def _sqlite_url_and_path(database_url: str) -> tuple[str, Path]:
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise UnsupportedDatabaseError(
            f"This Rotor release supports file-backed SQLite only, but "
            f"DATABASE_URL uses the '{parsed.drivername}' driver. Startup "
            f"migrations would be skipped, leaving the schema incomplete. "
            f"Set DATABASE_URL to a SQLite file URL such as "
            f"sqlite+aiosqlite:///~/.cache/rotor/rotor.db, or run an older "
            f"release that supports this driver."
        )
    if parsed.database in (None, "", ":memory:"):
        raise UnsupportedDatabaseError(
            "This Rotor release supports file-backed SQLite only, but "
            "DATABASE_URL does not name a database file. Use a file URL such as "
            "sqlite+aiosqlite:///~/.cache/rotor/rotor.db."
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


def _normalize_difference(
    difference: object,
) -> tuple[object, ...] | None:
    """Return one Alembic diff tuple from either supported diff shape.

    ``compare_metadata`` emits table/index changes as tuples but groups column
    changes in a one-item list.  Normalizing that representation lets the
    legacy-schema classifier apply one strict allowlist to both forms.
    """
    if isinstance(difference, tuple):
        return difference
    if (
        isinstance(difference, list)
        and len(difference) == 1
        and isinstance(difference[0], tuple)
    ):
        return difference[0]
    return None


def _schema_parity_signature(
    difference: tuple[object, ...],
) -> tuple[str, str, int] | None:
    """Identify one of the explicitly supported legacy type differences."""
    if len(difference) != 7 or difference[0] != "modify_type":
        return None

    _operation, schema, table_name, column_name, _details, old_type, new_type = (
        difference
    )
    if schema is not None:
        return None
    if type(old_type) not in (Text, TEXT) or getattr(old_type, "length", None) is not None:
        return None
    if type(new_type) is not String or not isinstance(column_name, str):
        return None
    length = getattr(new_type, "length", None)
    if not isinstance(table_name, str) or type(length) is not int:
        return None

    signature = (table_name, column_name, length)
    if signature not in _SCHEMA_PARITY_TYPE_CHANGES:
        return None
    return signature


def _legacy_revision(connection: Connection) -> str:
    _validate_user_schema_generation(connection)
    _validate_account_scope_generation(connection)
    _validate_client_oauth_binding_generation(connection)
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True},
    )
    differences = compare_metadata(context, Base.metadata)
    if not differences:
        return "head"

    normalized = [
        _normalize_difference(difference) for difference in differences
    ]
    if any(difference is None for difference in normalized):
        raise LegacyDatabaseError(
            "cannot safely adopt unversioned SQLite database: "
            "its schema does not match a known Rotor release"
        )

    # The cast above is guarded by the check immediately above.  Keeping the
    # concrete list makes the allowlist checks below explicit and type-safe.
    diff_tuples = [difference for difference in normalized if difference is not None]
    parity_signatures: list[tuple[str, str, int]] = []
    admin_additions = 0
    oauth_columns = []
    oauth_table_additions = 0
    callback_columns = []
    user_additions = False
    client_oauth_additions = set()
    client_oauth_index_tables = set()
    account_scope_additions = False
    client_oauth_binding_removals = set()
    for difference in diff_tuples:
        signature = _schema_parity_signature(difference)
        if signature is not None:
            parity_signatures.append(signature)
        elif _is_client_oauth_binding_removal(difference):
            table = difference[2] if difference[0] == "remove_column" else difference[1].table.name
            client_oauth_binding_removals.add((difference[0], table))
        elif _is_account_scope_addition(difference):
            account_scope_additions = True
        elif difference[0] == "add_table" and getattr(difference[1], "name", None) in _CLIENT_OAUTH_TABLES:
            client_oauth_additions.add(difference[1].name)
        elif difference[0] == "add_index" and getattr(getattr(difference[1], "table", None), "name", None) in _CLIENT_OAUTH_TABLES:
            client_oauth_index_tables.add(difference[1].table.name)
        elif _is_user_addition(difference):
            if "user_invitations" in inspect(connection).get_table_names():
                raise LegacyDatabaseError("cannot safely adopt unversioned SQLite database: partial user ownership constraints")
            user_additions = True
        elif _is_admin_addition(difference):
            admin_additions += 1
        elif (
            len(difference) == 4 and difference[0] == "add_column"
            and difference[1] is None and difference[2] == "channels"
            and getattr(difference[3], "name", None) in _OAUTH_CHANNEL_COLUMNS
        ):
            oauth_columns.append(difference[3].name)
        elif difference[0] == "add_table" and getattr(difference[1], "name", None) == "channel_oauth_sessions":
            oauth_table_additions += 1
        elif (
            len(difference) == 4 and difference[0] == "add_column"
            and difference[1] is None and difference[2] == "channel_oauth_sessions"
            and getattr(difference[3], "name", None) in _CALLBACK_SESSION_COLUMNS
        ):
            callback_columns.append(difference[3].name)
        else:
            raise LegacyDatabaseError(
                "cannot safely adopt unversioned SQLite database: "
                "its schema does not match a known Rotor release"
            )

    if ((client_oauth_additions and client_oauth_additions != _CLIENT_OAUTH_TABLES)
            or not client_oauth_index_tables <= client_oauth_additions):
        raise LegacyDatabaseError("cannot safely adopt unversioned SQLite database: partial client OAuth schema")
    if client_oauth_binding_removals and client_oauth_binding_removals != {
        (operation, table) for operation in ("remove_column", "remove_fk") for table in _CLIENT_OAUTH_BINDING_TABLES
    }:
        raise LegacyDatabaseError("cannot safely adopt unversioned SQLite database: partial client OAuth Key binding schema")

    if oauth_columns or oauth_table_additions:
        if set(oauth_columns) != _OAUTH_CHANNEL_COLUMNS or len(oauth_columns) != len(_OAUTH_CHANNEL_COLUMNS) or oauth_table_additions != 1:
            raise LegacyDatabaseError(
                "cannot safely adopt unversioned SQLite database: "
                "its schema does not match a known Rotor release"
            )
        if not parity_signatures and not admin_additions:
            return _PRE_OAUTH_REVISION

    if callback_columns:
        if set(callback_columns) != _CALLBACK_SESSION_COLUMNS or len(callback_columns) != len(_CALLBACK_SESSION_COLUMNS):
            raise LegacyDatabaseError(
                "cannot safely adopt unversioned SQLite database: "
                "its schema does not match a known Rotor release"
            )
        if not parity_signatures and not admin_additions:
            return _PRE_CALLBACK_REVISION

    if parity_signatures:
        # Require the complete four-column set exactly once.  A partial set,
        # duplicate operation, or any other type change must not be guessed.
        if (
            len(parity_signatures) != len(_SCHEMA_PARITY_TYPE_CHANGES)
            or set(parity_signatures) != _SCHEMA_PARITY_TYPE_CHANGES
        ):
            raise LegacyDatabaseError(
                "cannot safely adopt unversioned SQLite database: "
                "its schema does not match a known Rotor release"
            )

        if admin_additions:
            # f6a7 is the revision immediately before all administrator
            # migrations.  Do not use it if any admin table already exists:
            # the subsequent migrations would try to create that table again.
            existing_admin_tables = _ADMIN_TABLES & set(
                inspect(connection).get_table_names()
            )
            if existing_admin_tables:
                raise LegacyDatabaseError(
                    "cannot safely adopt unversioned SQLite database: "
                    "its schema does not match a known Rotor release"
                )
            return _PRE_ADMIN_REVISION
        return _PRE_SCHEMA_PARITY_REVISION

    if admin_additions:
        existing_admin_tables = _ADMIN_TABLES & set(
            inspect(connection).get_table_names()
        )
        if existing_admin_tables:
            raise LegacyDatabaseError(
                "cannot safely adopt unversioned SQLite database: "
                "its schema does not match a known Rotor release"
            )
        return _PRE_ADMIN_REVISION

    if user_additions:
        return _PRE_USER_REVISION
    if client_oauth_additions:
        return _PRE_CLIENT_OAUTH_REVISION
    if account_scope_additions:
        return _PRE_ACCOUNT_REVISION
    if client_oauth_binding_removals:
        return _PRE_ACCOUNT_OAUTH_REVISION
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
            _validate_account_scope_generation(connection)
            _validate_client_oauth_binding_generation(connection)
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
