"""widen communication_logs from/to address columns for email

Revision ID: 1a2b3c4d5e6f
Revises: 7c3ad02f9e11
Create Date: 2026-09-09 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "1a2b3c4d5e6f"
down_revision = "7c3ad02f9e11"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("communication_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "from_address", existing_type=sa.String(length=60), type_=sa.String(length=320)
        )
        batch_op.alter_column(
            "to_address", existing_type=sa.String(length=60), type_=sa.String(length=320)
        )


def downgrade():
    with op.batch_alter_table("communication_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "from_address", existing_type=sa.String(length=320), type_=sa.String(length=60)
        )
        batch_op.alter_column(
            "to_address", existing_type=sa.String(length=320), type_=sa.String(length=60)
        )
