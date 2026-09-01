"""Bring usage facts and credential indexes in sync with the models.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-31 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COMMON_USAGE_FACT_COLUMNS = (
    ("uncached_input_tokens", sa.Integer(), False, sa.text("0")),
    ("cache_write_tokens", sa.Integer(), False, sa.text("0")),
    ("cache_write_5m_tokens", sa.Integer(), False, sa.text("0")),
    ("cache_write_1h_tokens", sa.Integer(), False, sa.text("0")),
    ("usage_schema_version", sa.String(length=10), False, sa.text("'1'")),
    ("capacity_snapshot", sa.JSON(), True, None),
    ("cache_scope", sa.String(length=100), True, None),
    ("capacity_scope", sa.String(length=100), True, None),
    ("billing_scope", sa.String(length=100), True, None),
    ("cost_status", sa.String(length=30), False, sa.text("'unknown'")),
    ("tariff_version", sa.String(length=100), True, None),
    ("tariff_period", sa.String(length=100), True, None),
    ("tariff_snapshot", sa.JSON(), True, None),
)
_REQUEST_LOG_COLUMNS = (
    *_COMMON_USAGE_FACT_COLUMNS[:9],
    ("currency", sa.String(length=10), False, sa.text("'USD'")),
    *_COMMON_USAGE_FACT_COLUMNS[9:],
)
_UNIQUE_INDEXES = (
    ("admin_sessions", "secret_hash"),
    ("admin_users", "username"),
    ("mcp_control_keys", "secret_hash"),
)


def _add_missing_columns(
    table_name: str,
    definitions: tuple[tuple[str, sa.types.TypeEngine, bool, object], ...],
) -> None:
    bind = op.get_bind()
    existing = {
        column["name"]
        for column in sa.inspect(bind).get_columns(table_name)
    }
    for name, column_type, nullable, server_default in definitions:
        if name in existing:
            continue
        op.add_column(
            table_name,
            sa.Column(
                name,
                column_type,
                nullable=nullable,
                server_default=server_default,
            ),
        )


def _normalize_legacy_text_columns(table_name: str) -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    expected_lengths = {
        "usage_schema_version": 10,
        "cost_status": 30,
    }
    for name, length in expected_lengths.items():
        column = columns.get(name)
        if column is None or getattr(column["type"], "length", None) == length:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                name,
                existing_type=column["type"],
                type_=sa.String(length=length),
                existing_nullable=False,
            )


def _ensure_unique_index(table_name: str, column_name: str) -> None:
    index_name = op.f(f"ix_{table_name}_{column_name}")
    indexes = {
        index["name"]: index
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    current = indexes.get(index_name)
    if current is not None and current.get("unique"):
        return
    if current is not None:
        op.drop_index(index_name, table_name=table_name)
    op.create_index(index_name, table_name, [column_name], unique=True)


def upgrade() -> None:
    _add_missing_columns("request_logs", _REQUEST_LOG_COLUMNS)
    _add_missing_columns("usage_ledger", _COMMON_USAGE_FACT_COLUMNS)
    _normalize_legacy_text_columns("request_logs")
    _normalize_legacy_text_columns("usage_ledger")
    for table_name, column_name in _UNIQUE_INDEXES:
        _ensure_unique_index(table_name, column_name)


def downgrade() -> None:
    for table_name, column_name in reversed(_UNIQUE_INDEXES):
        index_name = op.f(f"ix_{table_name}_{column_name}")
        op.drop_index(index_name, table_name=table_name)
        op.create_index(index_name, table_name, [column_name], unique=False)

    for table_name, definitions in (
        ("usage_ledger", _COMMON_USAGE_FACT_COLUMNS),
        ("request_logs", _REQUEST_LOG_COLUMNS),
    ):
        existing = {
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns(table_name)
        }
        with op.batch_alter_table(table_name) as batch_op:
            for name, _column_type, _nullable, _server_default in reversed(
                definitions
            ):
                if name in existing:
                    batch_op.drop_column(name)
