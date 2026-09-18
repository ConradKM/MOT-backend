"""Add idempotency key for public deposit attempts.

Revision ID: a31d84ce9287
Revises: 5ba07cf3f21d
"""

import sqlalchemy as sa
from alembic import op

revision = "a31d84ce9287"
down_revision = "5ba07cf3f21d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("booking_requests", sa.Column("payment_attempt_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_booking_requests_payment_attempt_id",
        "booking_requests",
        ["payment_attempt_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_booking_requests_payment_attempt_id", table_name="booking_requests")
    op.drop_column("booking_requests", "payment_attempt_id")
