"""Add roles and multi role employees

Revision ID: 7c7e1ede0e3e
Revises: de3750c5d620
Create Date: 2026-09-01 23:57:24.791581

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c7e1ede0e3e'
down_revision = 'de3750c5d620'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('roles',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('garage_id', 'name', name='uq_roles_garage_id_name')
    )
    with op.batch_alter_table('roles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_roles_garage_id'), ['garage_id'], unique=False)

    op.create_table('employee_roles',
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('employee_id', 'role_id')
    )

    # --- employees.role: single string -> employee_roles (many-to-many) ---
    #
    # Seed OWNER + STAFF as real per-garage rows for every garage that
    # already exists (new garages get the same two seeded at registration
    # time instead, see auth/routes.py), then point every existing
    # employee's role at the matching row in their own garage, before
    # dropping the old column. Mirrors the appointment_type backfill in
    # migration 67b0dd2e6f6e.
    op.execute(
        """
        INSERT INTO roles (id, garage_id, name, created_at, updated_at)
        SELECT gen_random_uuid(), g.id, t.name, now(), now()
        FROM garages g
        CROSS JOIN (VALUES ('OWNER'), ('STAFF')) AS t(name)
        """
    )

    op.execute(
        """
        INSERT INTO employee_roles (employee_id, role_id)
        SELECT e.id, r.id
        FROM employees e
        JOIN roles r ON r.garage_id = e.garage_id AND r.name = e.role
        """
    )

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('role')


def downgrade():
    # Not supported, for the same reason 67b0dd2e6f6e's downgrade isn't:
    # employees.role is gone and role assignments now live in a
    # many-to-many table with no single value to collapse back into it
    # (an employee with zero or multiple roles has nothing to restore to).
    # Restore from a backup taken before this migration ran instead.
    raise NotImplementedError(
        "This migration is not reversible: employees.role was dropped in "
        "favor of a many-to-many roles table, and there's no single value "
        "to collapse a multi-role (or no-role) employee back into."
    )
