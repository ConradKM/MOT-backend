"""whatsapp_conversation_states - staff archive / soft-delete for the inbox

Revision ID: 7c3ad02f9e11
Revises: 5b1e9d4c7a20
Create Date: 2026-09-09 17:10:00.000000

One optional row per (garage, phone). Its absence = a normal active inbox
thread. Never touches message history or Twilio.
"""

import sqlalchemy as sa
from alembic import op

revision = "7c3ad02f9e11"
down_revision = "5b1e9d4c7a20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whatsapp_conversation_states",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("phone_e164", sa.String(length=40), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["garage_id"],
            ["garages.id"],
            name=op.f("fk_whatsapp_conversation_states_garage_id_garages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_conversation_states")),
        sa.UniqueConstraint(
            "garage_id", "phone_e164", name="uq_whatsapp_conversation_states_garage_phone"
        ),
    )
    op.create_index(
        op.f("ix_whatsapp_conversation_states_garage_id"),
        "whatsapp_conversation_states",
        ["garage_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_whatsapp_conversation_states_garage_id"),
        table_name="whatsapp_conversation_states",
    )
    op.drop_table("whatsapp_conversation_states")
