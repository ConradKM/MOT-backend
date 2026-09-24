"""Add immutable service-name snapshots to booking records.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable for historical records: their type relationship remains the
    # safe display fallback because their original label is unrecoverable.
    op.add_column(
        "appointments", sa.Column("appointment_type_name_at_booking", sa.String(length=100))
    )
    op.add_column(
        "booking_requests", sa.Column("requested_appointment_type_name", sa.String(length=100))
    )


def downgrade():
    op.drop_column("booking_requests", "requested_appointment_type_name")
    op.drop_column("appointments", "appointment_type_name_at_booking")
