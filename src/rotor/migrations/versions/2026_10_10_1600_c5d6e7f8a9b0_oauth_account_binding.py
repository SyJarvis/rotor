"""Bind client OAuth credentials directly to accounts instead of API Keys."""

from alembic import op
import sqlalchemy as sa


revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None

TABLES = ("client_oauth_codes", "client_oauth_grants")
NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def upgrade():
    from rotor.migrations.runner import _validate_client_oauth_binding_generation

    if not _validate_client_oauth_binding_generation(op.get_bind()):
        return
    for table in TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            batch.drop_constraint(f"fk_{table}_token_id_tokens", type_="foreignkey")
            batch.drop_column("token_id")


def downgrade():
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot downgrade account-bound OAuth authorization data; restore a pre-upgrade backup instead")
    for table in TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            batch.add_column(sa.Column("token_id", sa.Integer(), nullable=False))
            batch.create_foreign_key(f"fk_{table}_token_id_tokens", "tokens", ["token_id"], ["id"], ondelete="CASCADE")
