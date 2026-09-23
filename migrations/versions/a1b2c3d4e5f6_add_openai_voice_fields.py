"""Add OpenAI Voice provisioning fields to communications onboarding

Revision ID: a1b2c3d4e5f6
Revises: f62d4e8a9b01
Create Date: 2026-09-23 22:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f62d4e8a9b01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garage_communications_onboarding", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "openai_voice_status",
                sa.String(length=40),
                server_default="NOT_STARTED",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("openai_voice_trunk_sid", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("openai_voice_last_error_code", sa.String(length=40), nullable=True)
        )
        batch_op.add_column(sa.Column("openai_voice_last_error_message", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("openai_voice_ready_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("garage_communications_onboarding", schema=None) as batch_op:
        batch_op.drop_column("openai_voice_ready_at")
        batch_op.drop_column("openai_voice_last_error_message")
        batch_op.drop_column("openai_voice_last_error_code")
        batch_op.drop_column("openai_voice_trunk_sid")
        batch_op.drop_column("openai_voice_status")
