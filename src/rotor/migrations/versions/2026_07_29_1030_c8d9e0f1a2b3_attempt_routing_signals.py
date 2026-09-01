"""Add explicit routing signals to request attempts.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-07-29 10:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "request_attempts",
        sa.Column("retry_same_channel", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "request_attempts",
        sa.Column("fallback_allowed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "request_attempts",
        sa.Column("retry_after_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("request_attempts", "retry_after_seconds")
    op.drop_column("request_attempts", "fallback_allowed")
    op.drop_column("request_attempts", "retry_same_channel")
