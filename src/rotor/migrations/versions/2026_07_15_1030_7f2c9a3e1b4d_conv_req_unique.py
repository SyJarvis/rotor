"""conversation_records unique on (conversation_id, request_id)

Revision ID: 7f2c9a3e1b4d
Revises: 1b0ea4219dd0
Create Date: 2026-07-15 10:30:00.000000

"""

from __future__ import annotations
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7f2c9a3e1b4d'
down_revision: Union[str, None] = '1b0ea4219dd0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop duplicate rows first, keeping the earliest insertion per
    # (conversation_id, request_id). Without this the unique constraint
    # below would fail on existing duplicates produced by fallback retries.
    op.execute(
        """
        DELETE FROM conversation_records
        WHERE id NOT IN (
            SELECT MIN(id) FROM conversation_records
            GROUP BY conversation_id, request_id
        )
        """
    )
    with op.batch_alter_table("conversation_records") as batch_op:
        batch_op.create_unique_constraint(
            "uq_conv_req",
            ["conversation_id", "request_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_records") as batch_op:
        batch_op.drop_constraint("uq_conv_req", type_="unique")
