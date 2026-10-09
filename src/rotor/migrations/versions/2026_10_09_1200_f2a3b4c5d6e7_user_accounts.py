"""Account ownership, shared quotas, invitations, and personal audit scope."""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    schema = sa.inspect(op.get_bind())
    admin_columns = {column["name"] for column in schema.get_columns("admin_users")}
    token_columns = {column["name"] for column in schema.get_columns("tokens")}
    # Unversioned databases built from current metadata may need older type
    # parity migrations first. Do not recreate account columns or change roles.
    if "role" in admin_columns and "owner_id" in token_columns and schema.has_table("user_invitations"):
        return
    for column in (
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("used_quota", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_channels", sa.JSON(), nullable=True),
        sa.Column("allowed_models", sa.JSON(), nullable=True),
        sa.Column("default_token_id", sa.Integer(), nullable=True),
    ):
        if column.name not in admin_columns:
            op.add_column("admin_users", column)
    # Only accounts that predate the user system are administrators.
    if "role" not in admin_columns:
        op.execute(sa.text("UPDATE admin_users SET role='admin'"))
    with op.batch_alter_table("tokens") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("is_base", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("allowed_models", sa.JSON(), nullable=True))
        batch.create_foreign_key("fk_tokens_owner_id", "admin_users", ["owner_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_tokens_owner_id", ["owner_id"])
        batch.create_index("uq_tokens_owner_base", ["owner_id"], unique=True, sqlite_where=sa.text("is_base = 1"))
    for table in ("request_logs", "usage_ledger", "conversation_records"):
        op.add_column(table, sa.Column("owner_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    op.create_table("user_invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id"), nullable=False),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("allowed_channels", sa.JSON(), nullable=True),
        sa.Column("allowed_models", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("admin_users.id"), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("user_invitations")
    for table in ("request_logs", "usage_ledger", "conversation_records"):
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        op.drop_column(table, "owner_id")
    with op.batch_alter_table("tokens") as batch:
        batch.drop_index("uq_tokens_owner_base")
        batch.drop_index("ix_tokens_owner_id")
        batch.drop_constraint("fk_tokens_owner_id", type_="foreignkey")
        for field in ("owner_id", "is_base", "allowed_models"):
            batch.drop_column(field)
    for field in ("role", "display_name", "quota", "used_quota", "allowed_channels", "allowed_models", "default_token_id"):
        op.drop_column("admin_users", field)
