"""Public-client OAuth authorization codes, grants, and hashed credentials."""

from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    expected = {"client_oauth_codes", "client_oauth_grants", "client_oauth_credentials"}
    existing = expected & set(sa.inspect(op.get_bind()).get_table_names())
    # Legacy metadata clones can already contain the complete current schema.
    # The startup runner validates that schema before stamping older revisions.
    if existing == expected:
        return
    if existing:
        raise RuntimeError("Cannot migrate a partial client OAuth schema")
    op.create_table("client_oauth_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("secret_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(43), nullable=False),
        sa.Column("account_auth_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table("client_oauth_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column("account_auth_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_client_oauth_grants_owner_id", "client_oauth_grants", ["owner_id"])
    op.create_table("client_oauth_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grant_id", sa.Integer(), sa.ForeignKey("client_oauth_grants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_client_oauth_credentials_grant_id", "client_oauth_credentials", ["grant_id"])


def downgrade():
    op.drop_table("client_oauth_credentials")
    op.drop_table("client_oauth_grants")
    op.drop_table("client_oauth_codes")
