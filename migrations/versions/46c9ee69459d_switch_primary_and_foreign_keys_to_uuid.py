"""Switch primary and foreign keys to UUID

Revision ID: 46c9ee69459d
Revises: e95df561a9d9
Create Date: 2026-08-29 00:21:40.503686

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '46c9ee69459d'
down_revision = 'e95df561a9d9'
branch_labels = None
depends_on = None


def upgrade():
    # Sequential integer ids leak enumeration and business-intelligence
    # info (see issue #7) - switching every table to opaque, random (v4)
    # UUID primary/foreign keys. There is no meaningful in-place ALTER for
    # this: Postgres has no cast from an existing integer value to a valid
    # UUID, so this drops and recreates every table rather than attempting
    # one. Every environment this has run against so far has 0 rows in
    # these tables; if that's ever not true, back up first - see downgrade().
    op.drop_table('reminders')
    with op.batch_alter_table('mot_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mot_records_vehicle_id'))
        batch_op.drop_index(batch_op.f('ix_mot_records_garage_id'))
    op.drop_table('mot_records')
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_appointments_vehicle_id'))
        batch_op.drop_index('ix_appointments_garage_id_start_time')
        batch_op.drop_index(batch_op.f('ix_appointments_garage_id'))
        batch_op.drop_index(batch_op.f('ix_appointments_employee_id'))
        batch_op.drop_index(batch_op.f('ix_appointments_customer_id'))
    op.drop_table('appointments')
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_vehicles_registration_number'))
        batch_op.drop_index(batch_op.f('ix_vehicles_garage_id'))
        batch_op.drop_index(batch_op.f('ix_vehicles_customer_id'))
    op.drop_table('vehicles')
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_employees_email'))
    op.drop_table('employees')
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customers_last_name'))
        batch_op.drop_index(batch_op.f('ix_customers_garage_id'))
        batch_op.drop_index(batch_op.f('ix_customers_first_name'))
    op.drop_table('customers')
    op.drop_table('garages')

    op.create_table('garages',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('address', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('customers',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('first_name', sa.String(length=100), nullable=False),
    sa.Column('last_name', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customers_first_name'), ['first_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_customers_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customers_last_name'), ['last_name'], unique=False)

    op.create_table('employees',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=30), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_employees_email'), ['email'], unique=True)

    op.create_table('vehicles',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('registration_number', sa.String(length=20), nullable=False),
    sa.Column('make', sa.String(length=100), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('current_mileage', sa.Integer(), nullable=True),
    sa.Column('mot_expiry_date', sa.Date(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('garage_id', 'registration_number', name='uq_vehicle_garage_registration')
    )
    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_vehicles_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_vehicles_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_vehicles_registration_number'), ['registration_number'], unique=False)

    op.create_table('appointments',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('vehicle_id', sa.Uuid(), nullable=True),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('appointment_type', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointments_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_employee_id'), ['employee_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index('ix_appointments_garage_id_start_time', ['garage_id', 'start_time'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_vehicle_id'), ['vehicle_id'], unique=False)

    op.create_table('mot_records',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('vehicle_id', sa.Uuid(), nullable=False),
    sa.Column('mot_date', sa.Date(), nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=False),
    sa.Column('result', sa.String(length=10), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('mot_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mot_records_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_mot_records_vehicle_id'), ['vehicle_id'], unique=False)

    op.create_table('reminders',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('vehicle_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_id', sa.Uuid(), nullable=True),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['appointment_id'], ['appointments.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    # Not supported. UUID values can't be mapped back to the original
    # sequential integers - there's no data to reconstruct them from, only
    # a pre-migration backup. This is the "breaking API change" the issue
    # itself calls out; it's a one-way cutover, not a reversible step.
    raise NotImplementedError(
        "This migration is not reversible: switching from sequential "
        "integer ids to random UUIDs discards the original integer "
        "values. Restore from a backup taken before this migration ran "
        "instead of downgrading."
    )
