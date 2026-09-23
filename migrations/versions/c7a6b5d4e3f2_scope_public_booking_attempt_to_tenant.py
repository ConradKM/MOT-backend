"""Scope public booking attempt idempotency to its tenant.

Revision ID: c7a6b5d4e3f2
Revises: f62d4e8a9b01
Create Date: 2026-09-23
"""

from alembic import op

revision = "c7a6b5d4e3f2"
down_revision = "f62d4e8a9b01"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_booking_requests_payment_attempt_id", table_name="booking_requests")
    op.create_index(
        "uq_booking_request_garage_payment_attempt",
        "booking_requests",
        ["garage_id", "payment_attempt_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_booking_request_garage_payment_attempt", table_name="booking_requests")
    op.create_index(
        "ix_booking_requests_payment_attempt_id",
        "booking_requests",
        ["payment_attempt_id"],
        unique=True,
    )
