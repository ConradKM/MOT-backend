"""Add garages.layout_variant (platform-controlled presentation key)

Revision ID: d1f2a3b4c5e6
Revises: c9a4e1b7f350
Create Date: 2026-09-04 13:30:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'd1f2a3b4c5e6'
down_revision = 'c9a4e1b7f350'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('garages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('layout_variant', sa.String(length=50), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('garages', schema=None) as batch_op:
        batch_op.drop_column('layout_variant')
