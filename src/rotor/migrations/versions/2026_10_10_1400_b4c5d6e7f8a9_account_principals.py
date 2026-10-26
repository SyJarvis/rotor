"""Account-scoped Responses state and session leases without API Keys."""

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None

TABLES = ("response_routes", "session_leases", "session_lease_events")
IDENTITY_CHECK = "(token_id IS NOT NULL AND owner_id IS NULL) OR (token_id IS NULL AND owner_id IS NOT NULL)"


def upgrade():
    from rotor.migrations.runner import _validate_account_scope_generation

    if _validate_account_scope_generation(op.get_bind()):
        return
    for table in TABLES:
        with op.batch_alter_table(table, naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }) as batch:
            batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
            batch.alter_column("token_id", existing_type=sa.Integer(), nullable=True)
            if table != "session_lease_events":
                batch.create_foreign_key(f"fk_{table}_owner_id_admin_users", "admin_users", ["owner_id"], ["id"], ondelete="CASCADE")
            batch.create_index(f"ix_{table}_owner_id", ["owner_id"])
            batch.create_check_constraint(f"ck_{table}_identity", IDENTITY_CHECK)
            if table == "response_routes":
                batch.create_unique_constraint("uq_response_routes_owner_response", ["owner_id", "response_id"])
            elif table == "session_leases":
                batch.create_unique_constraint("uq_session_leases_owner_session_model", ["owner_id", "session_id", "logical_model"])


def downgrade():
    connection = op.get_bind()
    for table in TABLES:
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} WHERE owner_id IS NOT NULL LIMIT 1")):
            raise RuntimeError("Cannot downgrade account-owned state to API Key ownership; restore a backup instead")
    for table in reversed(TABLES):
        with op.batch_alter_table(table, naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }) as batch:
            if table == "response_routes":
                batch.drop_constraint("uq_response_routes_owner_response", type_="unique")
            elif table == "session_leases":
                batch.drop_constraint("uq_session_leases_owner_session_model", type_="unique")
            batch.drop_constraint(f"ck_{table}_identity", type_="check")
            batch.drop_index(f"ix_{table}_owner_id")
            if table != "session_lease_events":
                batch.drop_constraint(f"fk_{table}_owner_id_admin_users", type_="foreignkey")
            batch.alter_column("token_id", existing_type=sa.Integer(), nullable=False)
            batch.drop_column("owner_id")
