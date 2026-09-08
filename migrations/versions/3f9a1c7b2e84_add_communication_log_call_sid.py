"""add communication_logs.call_sid for grouping voice transcript turns under a call

Revision ID: 3f9a1c7b2e84
Revises: 1dfd3c9a2560
Create Date: 2026-09-08 18:20:00.000000

Every ConversationRelay call writes one CommunicationLog row per transcript
turn. Without a shared key those turns were each counted as a call. This
adds a nullable, indexed ``call_sid`` set on both the call-level row and its
engine turn rows so a call's turns group under it. WhatsApp/SMS/email rows
leave it null and are unaffected.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "3f9a1c7b2e84"
down_revision = "1dfd3c9a2560"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "communication_logs",
        sa.Column("call_sid", sa.String(length=40), nullable=True),
    )
    op.create_index(
        op.f("ix_communication_logs_call_sid"),
        "communication_logs",
        ["call_sid"],
        unique=False,
    )
    op.create_index(
        "ix_communication_logs_garage_id_call_sid",
        "communication_logs",
        ["garage_id", "call_sid"],
        unique=False,
    )

    # Backfill existing rows so the corrected Communications counts are right
    # immediately, not only for calls placed after this deploy. Postgres-only
    # (split_part); a SQLite dev upgrade just skips it.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The one call-level row per physical call already carries the
        # CallSid in external_id.
        op.execute(
            """
            UPDATE communication_logs
               SET call_sid = external_id
             WHERE channel = 'VOICE'
               AND external_provider <> 'comaz_conversation_engine'
               AND external_id IS NOT NULL
               AND call_sid IS NULL
            """
        )
        # Engine inbound turn rows carry "<CallSid>:<turn>" in external_id.
        op.execute(
            """
            UPDATE communication_logs
               SET call_sid = split_part(external_id, ':', 1)
             WHERE channel = 'VOICE'
               AND external_provider = 'comaz_conversation_engine'
               AND external_id LIKE '%:%'
               AND call_sid IS NULL
            """
        )


def downgrade():
    op.drop_index("ix_communication_logs_garage_id_call_sid", table_name="communication_logs")
    op.drop_index(op.f("ix_communication_logs_call_sid"), table_name="communication_logs")
    op.drop_column("communication_logs", "call_sid")
