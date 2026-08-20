"""Rename users to employees and rebuild appointments

Revision ID: e95df561a9d9
Revises: 603edbba4277
Create Date: 2026-08-20 15:45:56.671006

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e95df561a9d9'
down_revision = '603edbba4277'
branch_labels = None
depends_on = None


def upgrade():
    # --- users -> employees: a pure rename, so existing rows/ids are kept. ---
    op.rename_table('users', 'employees')
    op.execute('ALTER SEQUENCE users_id_seq RENAME TO employees_id_seq')
    op.execute('ALTER INDEX users_pkey RENAME TO employees_pkey')
    op.execute('ALTER INDEX ix_users_email RENAME TO ix_employees_email')
    op.execute(
        'ALTER TABLE employees RENAME CONSTRAINT users_garage_id_fkey '
        'TO employees_garage_id_fkey'
    )

    # --- appointments: rebuild for the real calendar/conflict-detection shape. ---
    # This table has never held any rows (the appointment endpoints did not
    # exist before this migration), so the NOT NULL additions below are safe
    # here; a system with real bookings would need a backfill step first.
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.alter_column('appointment_date', new_column_name='start_time')
        batch_op.add_column(sa.Column('end_time', sa.DateTime(timezone=True), nullable=False))
        batch_op.add_column(sa.Column('employee_id', sa.Integer(), nullable=False))
        batch_op.add_column(
            sa.Column(
                'created_at',
                sa.DateTime(timezone=True),
                server_default=sa.text('now()'),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                'updated_at',
                sa.DateTime(timezone=True),
                server_default=sa.text('now()'),
                nullable=False,
            )
        )
        batch_op.alter_column('vehicle_id', existing_type=sa.INTEGER(), nullable=True)

        batch_op.drop_constraint(batch_op.f('appointments_garage_id_fkey'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('appointments_customer_id_fkey'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('appointments_vehicle_id_fkey'), type_='foreignkey')
        batch_op.create_foreign_key(None, 'garages', ['garage_id'], ['id'], ondelete='CASCADE')
        batch_op.create_foreign_key(None, 'employees', ['employee_id'], ['id'], ondelete='CASCADE')
        batch_op.create_foreign_key(None, 'customers', ['customer_id'], ['id'], ondelete='CASCADE')
        batch_op.create_foreign_key(None, 'vehicles', ['vehicle_id'], ['id'], ondelete='SET NULL')

        batch_op.create_index(batch_op.f('ix_appointments_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_employee_id'), ['employee_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointments_vehicle_id'), ['vehicle_id'], unique=False)
        batch_op.create_index(
            'ix_appointments_garage_id_start_time', ['garage_id', 'start_time'], unique=False
        )


def downgrade():
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_index('ix_appointments_garage_id_start_time')
        batch_op.drop_index(batch_op.f('ix_appointments_vehicle_id'))
        batch_op.drop_index(batch_op.f('ix_appointments_customer_id'))
        batch_op.drop_index(batch_op.f('ix_appointments_employee_id'))
        batch_op.drop_index(batch_op.f('ix_appointments_garage_id'))

        batch_op.drop_constraint(batch_op.f('appointments_garage_id_fkey'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('appointments_employee_id_fkey'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('appointments_customer_id_fkey'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('appointments_vehicle_id_fkey'), type_='foreignkey')
        batch_op.create_foreign_key(
            batch_op.f('appointments_vehicle_id_fkey'), 'vehicles', ['vehicle_id'], ['id']
        )
        batch_op.create_foreign_key(
            batch_op.f('appointments_customer_id_fkey'), 'customers', ['customer_id'], ['id']
        )
        batch_op.create_foreign_key(
            batch_op.f('appointments_garage_id_fkey'), 'garages', ['garage_id'], ['id']
        )

        batch_op.alter_column('vehicle_id', existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')
        batch_op.drop_column('employee_id')
        batch_op.drop_column('end_time')
        batch_op.alter_column('start_time', new_column_name='appointment_date')

    op.execute(
        'ALTER TABLE employees RENAME CONSTRAINT employees_garage_id_fkey '
        'TO users_garage_id_fkey'
    )
    op.execute('ALTER INDEX ix_employees_email RENAME TO ix_users_email')
    op.execute('ALTER INDEX employees_pkey RENAME TO users_pkey')
    op.execute('ALTER SEQUENCE employees_id_seq RENAME TO users_id_seq')
    op.rename_table('employees', 'users')
