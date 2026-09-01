"""Add administrator login throttles and authentication audit events.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-25 13:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_login_throttles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username_key", sa.String(length=100), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=False),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "username_key",
            "client_ip",
            name="uq_admin_login_throttle_username_ip",
        ),
    )
    for column in (
        "id",
        "username_key",
        "client_ip",
        "locked_until",
    ):
        op.create_index(
            op.f(f"ix_admin_login_throttles_{column}"),
            "admin_login_throttles",
            [column],
        )

    op.create_table(
        "admin_auth_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "id",
        "admin_user_id",
        "username",
        "event_type",
        "client_ip",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_admin_auth_events_{column}"),
            "admin_auth_events",
            [column],
        )


def downgrade() -> None:
    op.drop_table("admin_auth_events")
    op.drop_table("admin_login_throttles")
