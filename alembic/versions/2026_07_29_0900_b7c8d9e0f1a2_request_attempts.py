"""add request attempt diagnostic records

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-29 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("requested_model", sa.String(length=100), nullable=False),
        sa.Column("provider_model", sa.String(length=100), nullable=True),
        sa.Column("request_protocol", sa.String(length=50), nullable=False),
        sa.Column("provider_protocol", sa.String(length=50), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=50), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("sanitized_error", sa.JSON(), nullable=True),
        sa.Column("provider_request_ids", sa.JSON(), nullable=False),
        sa.Column(
            "request_origin",
            sa.String(length=30),
            nullable=False,
            server_default="client",
        ),
        sa.Column("agent_run_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "attempt_index",
            name="uq_request_attempt_index",
        ),
    )
    for column in (
        "id",
        "request_id",
        "channel_id",
        "requested_model",
        "started_at",
        "outcome",
        "error_category",
        "request_origin",
        "agent_run_id",
    ):
        op.create_index(
            op.f(f"ix_request_attempts_{column}"),
            "request_attempts",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("request_attempts")
