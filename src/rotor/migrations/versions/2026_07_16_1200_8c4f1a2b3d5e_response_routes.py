"""add persistent native Responses routing

Revision ID: 8c4f1a2b3d5e
Revises: 7f2c9a3e1b4d
Create Date: 2026-07-16 12:00:00.000000
"""

from __future__ import annotations
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c4f1a2b3d5e"
down_revision: Union[str, None] = "7f2c9a3e1b4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "response_routes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("response_id", sa.String(length=200), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column(
            "usage_accounted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_id", "response_id", name="uq_response_routes_token_response"
        ),
    )
    for column in ("id", "response_id", "token_id", "channel_id", "status"):
        op.create_index(
            op.f(f"ix_response_routes_{column}"),
            "response_routes",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("response_routes")
