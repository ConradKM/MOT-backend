"""Tenant archive lifecycle + safe permanent delete

Two changes needed to close a real gap: the Platform Admin console
(comaz-admin) already ships archive/unarchive/permanent-delete UI and calls
`/tenants/<id>/archive`, `/tenants/<id>/unarchive` and `DELETE
/tenants/<id>` - none of which existed on the backend, so every one of those
buttons 404'd in production.

* ``garages`` gains ``archive_reason`` (nullable Text), mirroring the
  existing ``suspension_reason`` - kept separate so a tenant that was
  suspended and later archived doesn't lose either reason.
* ``reminders.garage_id`` was the one foreign key into ``garages`` still
  ``ON DELETE NO ACTION`` (every sibling table - appointments, customers,
  vehicles, booking_requests, communication_logs, etc. - is already
  ``ON DELETE CASCADE``). Left as-is, permanently deleting any tenant with an
  outstanding MOT reminder would fail with a foreign key violation. Brought
  in line with every other tenant-owned table.

``platform_audit_logs.garage_id`` is already ``ON DELETE SET NULL`` - a
deleted tenant's audit trail survives (record_audit also snapshots the name
onto ``garage_name``, a plain column, before the delete happens).

Revision ID: c2e4a8f61d93
Revises: a1f3c9d02b77
Create Date: 2026-09-11 17:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c2e4a8f61d93"
down_revision = "a1f3c9d02b77"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archive_reason", sa.Text(), nullable=True))

    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.drop_constraint("reminders_garage_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_garage_id_fkey", "garages", ["garage_id"], ["id"], ondelete="CASCADE"
        )


def downgrade():
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.drop_constraint("reminders_garage_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key("reminders_garage_id_fkey", "garages", ["garage_id"], ["id"])

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("archive_reason")
