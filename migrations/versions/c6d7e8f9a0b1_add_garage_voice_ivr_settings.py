"""Add garage_voice_ivr_settings - a business's own incoming-call menu.

Purely additive: no existing table or column changes. A business with no row
(every business at upgrade time) keeps today's inbound call behaviour
exactly; downgrade drops the table.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "garage_voice_ivr_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("greeting", sa.String(length=500), nullable=True),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column(
            "fallback_action",
            sa.String(length=30),
            server_default="HUMAN_TRANSFER",
            nullable=False,
        ),
        sa.Column("fallback_target", sa.String(length=20), nullable=True),
        sa.Column("max_attempts", sa.Integer(), server_default="2", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_garage_voice_ivr_settings_garage_id"),
        "garage_voice_ivr_settings",
        ["garage_id"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        op.f("ix_garage_voice_ivr_settings_garage_id"), table_name="garage_voice_ivr_settings"
    )
    op.drop_table("garage_voice_ivr_settings")
