"""add adaptive routing decision records

Revision ID: a1b2c3d4e5f6
Revises: 8c4f1a2b3d5e
Create Date: 2026-07-20 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "8c4f1a2b3d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("request_protocol", sa.String(length=50), nullable=False),
        sa.Column("strategy", sa.String(length=50), nullable=False),
        sa.Column(
            "policy_version", sa.String(length=30), nullable=False,
            server_default="adaptive-v1",
        ),
        sa.Column("candidate_channel_ids", sa.JSON(), nullable=False),
        sa.Column("selected_channel_id", sa.Integer(), nullable=True),
        sa.Column("required_capabilities", sa.JSON(), nullable=False),
        sa.Column("affinity_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("score_snapshot", sa.JSON(), nullable=True),
        sa.Column("feature_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "id", "request_id", "token_id", "model", "strategy",
        "selected_channel_id", "created_at",
    ):
        op.create_index(
            op.f(f"ix_routing_decisions_{column}"),
            "routing_decisions",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("routing_decisions")
