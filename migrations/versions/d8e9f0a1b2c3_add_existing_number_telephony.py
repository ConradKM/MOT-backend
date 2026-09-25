"""Existing-number telephony: telephony mode, public number, BYOC routing
identity and ordered human handoff destinations.

Purely additive and nullable. Every existing business keeps NULL everywhere,
which the application treats exactly as before (a CoMaz-owned number, human
escalation via voice_escalation_number / voice_fallback_number).

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None

TABLE = "garage_communication_settings"


def upgrade():
    op.add_column(TABLE, sa.Column("telephony_mode", sa.String(length=20), nullable=True))
    op.add_column(TABLE, sa.Column("public_business_number", sa.String(length=20), nullable=True))
    op.add_column(TABLE, sa.Column("byoc_trunk_sid", sa.String(length=64), nullable=True))
    op.add_column(TABLE, sa.Column("byoc_sip_domain_sid", sa.String(length=64), nullable=True))
    op.add_column(TABLE, sa.Column("human_primary_type", sa.String(length=20), nullable=True))
    op.add_column(
        TABLE, sa.Column("human_primary_destination", sa.String(length=255), nullable=True)
    )
    op.add_column(TABLE, sa.Column("human_secondary_type", sa.String(length=20), nullable=True))
    op.add_column(
        TABLE, sa.Column("human_secondary_destination", sa.String(length=255), nullable=True)
    )
    op.add_column(TABLE, sa.Column("human_transfer_timeout_seconds", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        "uq_garage_communication_settings_public_number", TABLE, ["public_business_number"]
    )


def downgrade():
    op.drop_constraint("uq_garage_communication_settings_public_number", TABLE, type_="unique")
    op.drop_column(TABLE, "human_transfer_timeout_seconds")
    op.drop_column(TABLE, "human_secondary_destination")
    op.drop_column(TABLE, "human_secondary_type")
    op.drop_column(TABLE, "human_primary_destination")
    op.drop_column(TABLE, "human_primary_type")
    op.drop_column(TABLE, "byoc_sip_domain_sid")
    op.drop_column(TABLE, "byoc_trunk_sid")
    op.drop_column(TABLE, "public_business_number")
    op.drop_column(TABLE, "telephony_mode")
