"""Add tenant-scoped AI voice FAQs.

Revision ID: a8c2e13f4b91
Revises: a31d84ce9287
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op

revision = "a8c2e13f4b91"
down_revision = "a31d84ce9287"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "garage_voice_faqs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.String(length=300), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_garage_voice_faqs_garage_id", "garage_voice_faqs", ["garage_id"])
    op.create_index("ix_garage_voice_faqs_archived_at", "garage_voice_faqs", ["archived_at"])


def downgrade():
    op.drop_index("ix_garage_voice_faqs_archived_at", table_name="garage_voice_faqs")
    op.drop_index("ix_garage_voice_faqs_garage_id", table_name="garage_voice_faqs")
    op.drop_table("garage_voice_faqs")
