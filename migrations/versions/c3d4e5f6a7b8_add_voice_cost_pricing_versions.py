"""Add openai_cost_currency and pricing-version columns to voice_call_metrics

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-23 23:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("voice_call_metrics", schema=None) as batch_op:
        batch_op.add_column(sa.Column("openai_cost_currency", sa.String(length=3), nullable=True))
        batch_op.add_column(
            sa.Column("openai_pricing_version", sa.String(length=60), nullable=True)
        )
        batch_op.add_column(
            sa.Column("twilio_pricing_version", sa.String(length=60), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("voice_call_metrics", schema=None) as batch_op:
        batch_op.drop_column("twilio_pricing_version")
        batch_op.drop_column("openai_pricing_version")
        batch_op.drop_column("openai_cost_currency")
