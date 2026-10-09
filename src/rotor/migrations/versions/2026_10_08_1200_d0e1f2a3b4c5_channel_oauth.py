"""Persist channel authentication choices and device login sessions.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-08 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


_CHANNEL_COLUMNS = (
    sa.Column("auth_type", sa.String(20), nullable=False, server_default="api_key"),
    sa.Column("oauth_access_token", sa.Text(), nullable=True),
    sa.Column("oauth_refresh_token", sa.Text(), nullable=True),
    sa.Column("oauth_expires_at", sa.Float(), nullable=True),
    sa.Column("oauth_account_id", sa.String(255), nullable=True),
    sa.Column("oauth_status", sa.String(20), nullable=True),
    sa.Column("oauth_error", sa.String(255), nullable=True),
    sa.Column("oauth_refresh_owner", sa.String(64), nullable=True),
    sa.Column("oauth_refresh_lock_until", sa.Float(), nullable=True),
)


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("channels")}
    for column in _CHANNEL_COLUMNS:
        if column.name not in existing:
            op.add_column("channels", sa.Column(
                column.name, column.type, nullable=column.nullable, server_default=column.server_default,
            ))
    # Known unversioned schemas can already contain this independently added
    # table while predating the administrator or usage parity migrations.
    if not sa.inspect(op.get_bind()).has_table("channel_oauth_sessions"):
        op.create_table(
            "channel_oauth_sessions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("admin_user_id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("device_auth_id", sa.Text(), nullable=True),
            sa.Column("user_code", sa.String(255), nullable=True),
            sa.Column("interval", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("next_poll_at", sa.Float(), nullable=False),
            sa.Column("poll_owner", sa.String(64), nullable=True),
            sa.Column("poll_lock_until", sa.Float(), nullable=True),
            sa.Column("access_token", sa.Text(), nullable=True),
            sa.Column("refresh_token", sa.Text(), nullable=True),
            sa.Column("token_expires_at", sa.Float(), nullable=True),
            sa.Column("account_id", sa.String(255), nullable=True),
            sa.Column("error", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("channel_oauth_sessions")
    with op.batch_alter_table("channels") as batch:
        for column in reversed(_CHANNEL_COLUMNS):
            batch.drop_column(column.name)
