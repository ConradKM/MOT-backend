"""Add persistent staff-only customer notes.

Existing customers receive NULL and retain their current behaviour. Notes are
intentionally stored on the tenant-scoped customer record rather than a
booking, so authorised staff can carry relevant context across appointments.

Revision ID: c7d8e9f0a1b2
Revises: c6d7e8f9a0b1
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7d8e9f0a1b2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("customers", sa.Column("notes", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("customers", "notes")
