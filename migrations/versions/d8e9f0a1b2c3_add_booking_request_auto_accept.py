"""Add tenant-scoped booking-request auto-accept controls.

Revision ID: a9b7c6d5e4f3
Revises: d8e9f0a1b2c3
"""

import sqlalchemy as sa
from alembic import op

revision = "a9b7c6d5e4f3"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "garages",
        sa.Column(
            "auto_accept_booking_requests", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "garages",
        sa.Column(
            "auto_accept_booking_requests_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "booking_requests",
        sa.Column(
            "accepted_automatically", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.drop_column("booking_requests", "accepted_automatically")
    op.drop_column("garages", "auto_accept_booking_requests_enabled")
    op.drop_column("garages", "auto_accept_booking_requests")
