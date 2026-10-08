"""Use browser PKCE authorization for channel login sessions.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-08 14:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def _clear_sessions() -> None:
    op.execute(sa.text(
        "UPDATE channel_oauth_sessions SET status='cancelled', "
        "error='Login method changed; start again', device_auth_id=NULL, user_code=NULL, "
        "access_token=NULL, refresh_token=NULL, token_expires_at=NULL, account_id=NULL, "
        "poll_owner=NULL, poll_lock_until=NULL, oauth_state=NULL, pkce_verifier=NULL "
        "WHERE status IN ('pending', 'exchanging', 'authorized')"
    ))


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("channel_oauth_sessions")}
    for name, length in (("oauth_state", 64), ("pkce_verifier", 128)):
        if name not in existing:
            op.add_column("channel_oauth_sessions", sa.Column(name, sa.String(length), nullable=True))
    _clear_sessions()


def downgrade() -> None:
    _clear_sessions()
    with op.batch_alter_table("channel_oauth_sessions") as batch:
        batch.drop_column("pkce_verifier")
        batch.drop_column("oauth_state")
