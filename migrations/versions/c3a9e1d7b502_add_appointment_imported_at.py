"""Mark appointments that came from a bulk import

Revision ID: c3a9e1d7b502
Revises: b7d2e4f6a813
Create Date: 2026-10-13 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3a9e1d7b502"
down_revision = "b7d2e4f6a813"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable, no default: every existing appointment was booked in the app,
    # which is exactly what NULL means.
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.drop_column("imported_at")
