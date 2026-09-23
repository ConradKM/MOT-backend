"""Add an opaque recovery capability for public deposit attempts.

Revision ID: e9f1a2b3c4d5
Revises: d8e3f7a2b9c1
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "e9f1a2b3c4d5"
down_revision = "d8e3f7a2b9c1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "booking_requests",
        sa.Column("payment_recovery_token_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_booking_requests_payment_recovery_token_hash",
        "booking_requests",
        ["payment_recovery_token_hash"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_booking_requests_payment_recovery_token_hash", table_name="booking_requests")
    op.drop_column("booking_requests", "payment_recovery_token_hash")
