"""Add address_city and address_region to Garage, for Twilio regulatory
address resources when buying a phone number.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("garages", sa.Column("address_city", sa.String(length=100)))
    op.add_column("garages", sa.Column("address_region", sa.String(length=100)))


def downgrade():
    op.drop_column("garages", "address_region")
    op.drop_column("garages", "address_city")
