"""Widen voice_call_metrics cost columns from Numeric(10,4) to Numeric(14,6)

A single-digit-second call's share of a per-minute rate is well under a
cent; 4 decimal places rounds that to noise, which distorts a total summed
across many short calls. 6 decimal places keeps that precision without
losing headroom on the whole-dollar side (14 total digits).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-24 00:15:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("voice_call_metrics", schema=None) as batch_op:
        batch_op.alter_column(
            "openai_cost_amount",
            existing_type=sa.Numeric(precision=10, scale=4),
            type_=sa.Numeric(precision=14, scale=6),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "twilio_cost_amount",
            existing_type=sa.Numeric(precision=10, scale=4),
            type_=sa.Numeric(precision=14, scale=6),
            existing_nullable=True,
        )


def downgrade():
    with op.batch_alter_table("voice_call_metrics", schema=None) as batch_op:
        batch_op.alter_column(
            "twilio_cost_amount",
            existing_type=sa.Numeric(precision=14, scale=6),
            type_=sa.Numeric(precision=10, scale=4),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "openai_cost_amount",
            existing_type=sa.Numeric(precision=14, scale=6),
            type_=sa.Numeric(precision=10, scale=4),
            existing_nullable=True,
        )
