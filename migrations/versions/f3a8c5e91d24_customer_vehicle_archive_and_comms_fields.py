"""Customer/vehicle archive flags, customer SMS opt-out, reminder delivery fields

Revision ID: f3a8c5e91d24
Revises: e2c7a91d4b60
Create Date: 2026-09-05 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'f3a8c5e91d24'
down_revision = 'e2c7a91d4b60'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False)
        )
        batch_op.add_column(
            sa.Column('sms_opt_out', sa.Boolean(), server_default=sa.text('false'), nullable=False)
        )

    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False)
        )

    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider_message_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_column('delivered_at')
        batch_op.drop_column('provider_message_id')

    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.drop_column('is_active')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('sms_opt_out')
        batch_op.drop_column('is_active')
