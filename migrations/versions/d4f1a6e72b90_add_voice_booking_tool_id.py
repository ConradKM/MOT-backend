"""Make voice booking tool retries idempotent.

Revision ID: d4f1a6e72b90
Revises: a8c2e13f4b91
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op

revision = "d4f1a6e72b90"
down_revision = "a8c2e13f4b91"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("booking_requests", sa.Column("voice_tool_call_id", sa.String(length=100)))
    op.create_index(
        "ix_booking_requests_voice_tool_call_id",
        "booking_requests",
        ["voice_tool_call_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_booking_requests_voice_tool_call_id", table_name="booking_requests")
    op.drop_column("booking_requests", "voice_tool_call_id")
