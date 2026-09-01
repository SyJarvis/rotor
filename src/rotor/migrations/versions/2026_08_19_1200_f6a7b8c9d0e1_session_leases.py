"""Add persistent Session Leases and transition events.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-19 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=100), nullable=False),
        sa.Column("logical_model", sa.String(length=100), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["token_id"], ["tokens.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_id",
            "session_id",
            "logical_model",
            name="uq_session_leases_token_session_model",
        ),
    )
    for column in (
        "id",
        "token_id",
        "logical_model",
        "channel_id",
        "last_used_at",
        "expires_at",
    ):
        op.create_index(
            op.f(f"ix_session_leases_{column}"),
            "session_leases",
            [column],
        )

    op.create_table(
        "session_lease_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=100), nullable=False),
        sa.Column("logical_model", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("previous_channel_id", sa.Integer(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ('assigned', 'renewed', 'migrated', 'expired')",
            name="ck_session_lease_events_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "id",
        "lease_id",
        "request_id",
        "token_id",
        "logical_model",
        "event_type",
        "previous_channel_id",
        "channel_id",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_session_lease_events_{column}"),
            "session_lease_events",
            [column],
        )


def downgrade() -> None:
    op.drop_table("session_lease_events")
    op.drop_table("session_leases")
