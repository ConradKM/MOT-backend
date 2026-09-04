"""Add garage scheduling (settings, opening hours, date exceptions)

Revision ID: a7c1e9f4d2b8
Revises: f4e8b2783149
Create Date: 2026-09-03 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7c1e9f4d2b8'
down_revision = 'f4e8b2783149'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'garage_schedule_settings',
        sa.Column('garage_id', sa.Uuid(), nullable=False),
        sa.Column('slot_interval_minutes', sa.Integer(), nullable=False),
        sa.Column('default_appointment_minutes', sa.Integer(), nullable=False),
        sa.Column('min_lead_time_hours', sa.Integer(), nullable=False),
        sa.Column('max_advance_days', sa.Integer(), nullable=False),
        sa.Column('capacity_per_slot', sa.Integer(), nullable=True),
        sa.Column('limited_threshold_ratio', sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('garage_id', name='uq_garage_schedule_settings_garage_id'),
    )
    with op.batch_alter_table('garage_schedule_settings', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_garage_schedule_settings_garage_id'), ['garage_id'], unique=False
        )

    op.create_table(
        'garage_opening_hours',
        sa.Column('garage_id', sa.Uuid(), nullable=False),
        sa.Column('weekday', sa.Integer(), nullable=False),
        sa.Column('opens_at', sa.Time(), nullable=False),
        sa.Column('closes_at', sa.Time(), nullable=False),
        sa.Column('is_closed', sa.Boolean(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('garage_id', 'weekday', name='uq_garage_opening_hours_garage_weekday'),
    )
    with op.batch_alter_table('garage_opening_hours', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_garage_opening_hours_garage_id'), ['garage_id'], unique=False
        )

    op.create_table(
        'garage_schedule_exceptions',
        sa.Column('garage_id', sa.Uuid(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('is_closed', sa.Boolean(), nullable=False),
        sa.Column('opens_at', sa.Time(), nullable=True),
        sa.Column('closes_at', sa.Time(), nullable=True),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('garage_id', 'date', name='uq_garage_schedule_exceptions_garage_date'),
    )
    with op.batch_alter_table('garage_schedule_exceptions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_garage_schedule_exceptions_garage_id'), ['garage_id'], unique=False
        )

    # Backfill defaults for every existing garage - kept in sync with
    # app/garages/schedule/defaults.py (new garages seed the same values at
    # registration).
    op.execute(
        """
        INSERT INTO garage_schedule_settings
            (id, garage_id, slot_interval_minutes, default_appointment_minutes,
             min_lead_time_hours, max_advance_days, capacity_per_slot,
             limited_threshold_ratio, created_at, updated_at)
        SELECT gen_random_uuid(), g.id, 30, 60, 24, 60, NULL, 0.5, now(), now()
        FROM garages g
        """
    )
    op.execute(
        """
        INSERT INTO garage_opening_hours
            (id, garage_id, weekday, opens_at, closes_at, is_closed, created_at, updated_at)
        SELECT gen_random_uuid(), g.id, d.weekday,
               TIME '09:00', TIME '17:00', d.is_closed, now(), now()
        FROM garages g
        CROSS JOIN (VALUES
            (0, false), (1, false), (2, false), (3, false),
            (4, false), (5, true), (6, true)
        ) AS d(weekday, is_closed)
        """
    )


def downgrade():
    with op.batch_alter_table('garage_schedule_exceptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_schedule_exceptions_garage_id'))
    op.drop_table('garage_schedule_exceptions')

    with op.batch_alter_table('garage_opening_hours', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_opening_hours_garage_id'))
    op.drop_table('garage_opening_hours')

    with op.batch_alter_table('garage_schedule_settings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_schedule_settings_garage_id'))
    op.drop_table('garage_schedule_settings')
