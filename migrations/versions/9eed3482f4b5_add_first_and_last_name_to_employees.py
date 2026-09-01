"""Add first and last name to employees

Revision ID: 9eed3482f4b5
Revises: 7c7e1ede0e3e
Create Date: 2026-09-02 00:22:15.515148

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9eed3482f4b5'
down_revision = '7c7e1ede0e3e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(sa.Column('first_name', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('last_name', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('last_name')
        batch_op.drop_column('first_name')
