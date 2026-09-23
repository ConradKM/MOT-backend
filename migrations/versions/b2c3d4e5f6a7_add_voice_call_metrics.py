"""Add voice_call_metrics table for AI voice call usage/cost/quality telemetry

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-23 23:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "voice_call_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("booking_request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "provider",
            sa.String(length=40),
            nullable=False,
            server_default="openai_realtime_sip",
        ),
        sa.Column("external_call_id", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("end_reason", sa.String(length=40), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("audio_input_seconds", sa.Integer(), nullable=True),
        sa.Column("audio_output_seconds", sa.Integer(), nullable=True),
        sa.Column("openai_cost_amount", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column(
            "openai_cost_is_estimated", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("twilio_cost_amount", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("twilio_cost_currency", sa.String(length=3), nullable=True),
        sa.Column(
            "twilio_cost_is_estimated", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("booking_outcome", sa.String(length=40), nullable=True),
        sa.Column("escalated_to_human", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["booking_request_id"], ["booking_requests.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_call_id"),
    )
    op.create_index(op.f("ix_voice_call_metrics_garage_id"), "voice_call_metrics", ["garage_id"])
    op.create_index(
        "ix_voice_call_metrics_garage_id_started_at",
        "voice_call_metrics",
        ["garage_id", "started_at"],
    )


def downgrade():
    op.drop_index("ix_voice_call_metrics_garage_id_started_at", table_name="voice_call_metrics")
    op.drop_index(op.f("ix_voice_call_metrics_garage_id"), table_name="voice_call_metrics")
    op.drop_table("voice_call_metrics")
