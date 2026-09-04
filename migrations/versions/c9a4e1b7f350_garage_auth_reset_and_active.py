"""Garage auth: employee is_active / tokens_valid_from + password_reset_tokens

Revision ID: c9a4e1b7f350
Revises: b3d5f8c21a90
Create Date: 2026-09-04 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'c9a4e1b7f350'
down_revision = 'b3d5f8c21a90'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'is_active',
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('true'),
            )
        )
        batch_op.add_column(
            sa.Column('tokens_valid_from', sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        'password_reset_tokens',
        sa.Column('employee_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_password_reset_tokens_token_hash'),
    )
    with op.batch_alter_table('password_reset_tokens', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_password_reset_tokens_employee_id'),
            ['employee_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_password_reset_tokens_token_hash'),
            ['token_hash'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('password_reset_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_password_reset_tokens_token_hash'))
        batch_op.drop_index(batch_op.f('ix_password_reset_tokens_employee_id'))
    op.drop_table('password_reset_tokens')

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('tokens_valid_from')
        batch_op.drop_column('is_active')
