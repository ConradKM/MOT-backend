"""MOT reminder settings + reminder-event columns + garage postcode/website

Revision ID: e2c7a91d4b60
Revises: d1f2a3b4c5e6
Create Date: 2026-09-04 16:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'e2c7a91d4b60'
down_revision = 'd1f2a3b4c5e6'
branch_labels = None
depends_on = None


def upgrade():
    # --- per-garage MOT reminder schedule -------------------------------
    op.create_table(
        'mot_reminder_settings',
        sa.Column('garage_id', sa.Uuid(), nullable=False),
        sa.Column('stage1_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('stage1_days_before', sa.Integer(), server_default='30', nullable=False),
        sa.Column('stage2_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('stage2_days_before', sa.Integer(), server_default='7', nullable=False),
        sa.Column('stage3_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('stage3_days_before', sa.Integer(), server_default='1', nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('garage_id', name='uq_mot_reminder_settings_garage_id'),
    )
    with op.batch_alter_table('mot_reminder_settings', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_mot_reminder_settings_garage_id'),
            ['garage_id'],
            unique=False,
        )

    # --- reminder-event columns (existing rows keep working) ------------
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('initiated_by_employee_id', sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'trigger', sa.String(length=20),
                server_default='AUTOMATIC', nullable=False,
            )
        )
        batch_op.add_column(sa.Column('stage', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('mot_expiry_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('detail', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'created_at', sa.DateTime(timezone=True),
                server_default=sa.text('now()'), nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                'updated_at', sa.DateTime(timezone=True),
                server_default=sa.text('now()'), nullable=False,
            )
        )
        batch_op.create_foreign_key(
            'fk_reminders_initiated_by_employee_id_employees',
            'employees', ['initiated_by_employee_id'], ['id'], ondelete='SET NULL',
        )

    # --- garage business details --------------------------------------
    with op.batch_alter_table('garages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('postcode', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('website', sa.String(length=200), nullable=True))


def downgrade():
    with op.batch_alter_table('garages', schema=None) as batch_op:
        batch_op.drop_column('website')
        batch_op.drop_column('postcode')

    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_reminders_initiated_by_employee_id_employees', type_='foreignkey'
        )
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')
        batch_op.drop_column('detail')
        batch_op.drop_column('mot_expiry_date')
        batch_op.drop_column('stage')
        batch_op.drop_column('trigger')
        batch_op.drop_column('initiated_by_employee_id')

    with op.batch_alter_table('mot_reminder_settings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mot_reminder_settings_garage_id'))
    op.drop_table('mot_reminder_settings')
