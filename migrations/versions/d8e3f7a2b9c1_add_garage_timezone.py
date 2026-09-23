"""Add the business timezone used by booking wall-clock slots.

Revision ID: d8e3f7a2b9c1
Revises: c7a6b5d4e3f2
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "d8e3f7a2b9c1"
down_revision = "c7a6b5d4e3f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "timezone",
                sa.String(length=64),
                nullable=False,
                server_default="Europe/London",
            )
        )


def downgrade():
    with op.batch_alter_table("garages") as batch_op:
        batch_op.drop_column("timezone")
