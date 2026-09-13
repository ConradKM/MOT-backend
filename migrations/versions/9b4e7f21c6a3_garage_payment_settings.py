"""garage payment settings: per-business payment provider selection

Revision ID: 9b4e7f21c6a3
Revises: 7e66710b3cf8
Create Date: 2026-09-13 09:00:00.000000

Purely additive: one new, entirely optional table. A garage with no row
here behaves exactly as every garage did before this migration - see
app/payments/settings.py::resolve_provider_name. Nothing existing is
touched.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9b4e7f21c6a3"
down_revision = "7e66710b3cf8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "garage_payment_settings",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("configuration_status", sa.String(length=20), nullable=False),
        sa.Column("live_mode", sa.Boolean(), nullable=False),
        sa.Column("merchant_account_reference", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("garage_id", name="uq_garage_payment_settings_garage_id"),
    )
    with op.batch_alter_table("garage_payment_settings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_garage_payment_settings_garage_id"), ["garage_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("garage_payment_settings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_garage_payment_settings_garage_id"))

    op.drop_table("garage_payment_settings")
