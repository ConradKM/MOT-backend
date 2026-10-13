"""Add the owner-chosen booking-page background and text colours

Revision ID: b7d2e4f6a813
Revises: 86b6c2593cdb
Create Date: 2026-10-12 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7d2e4f6a813"
down_revision = "86b6c2593cdb"
branch_labels = None
depends_on = None


def upgrade():
    # Both nullable with no default: NULL is "the default look", which is what
    # every existing business keeps until its owner picks colours.
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("booking_background_color", sa.String(length=7), nullable=True)
        )
        batch_op.add_column(sa.Column("booking_text_color", sa.String(length=7), nullable=True))


def downgrade():
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("booking_text_color")
        batch_op.drop_column("booking_background_color")
