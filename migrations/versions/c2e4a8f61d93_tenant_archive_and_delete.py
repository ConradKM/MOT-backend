"""Tenant archive lifecycle + safe permanent delete

Two changes needed to close a real gap: the Platform Admin console
(comaz-admin) already ships archive/unarchive/permanent-delete UI and calls
`/tenants/<id>/archive`, `/tenants/<id>/unarchive` and `DELETE
/tenants/<id>` - none of which existed on the backend, so every one of those
buttons 404'd in production.

* ``garages`` gains ``archive_reason`` (nullable Text), mirroring the
  existing ``suspension_reason`` - kept separate so a tenant that was
  suspended and later archived doesn't lose either reason.
* Every one of ``reminders``' own foreign keys was still ``ON DELETE NO
  ACTION`` - not just ``garage_id``, but ``customer_id``, ``vehicle_id`` and
  ``appointment_id`` too. CI caught the second layer this migration's first
  draft missed: fixing ``garage_id`` alone still failed, because deleting a
  tenant cascades into deleting its customers and vehicles (both already
  ``ON DELETE CASCADE`` from ``garages``), and *that* delete then hit
  ``reminders_vehicle_id_fkey`` / ``reminders_customer_id_fkey``, still
  ``NO ACTION``. ``customer_id`` and ``vehicle_id`` are ``NOT NULL`` on
  ``Reminder`` (see ``app/models/reminder.py``), so they become ``CASCADE``
  - the same rule ``mot_records.vehicle_id`` and ``appointments.customer_id``
  already use. ``appointment_id`` is nullable, so it becomes ``SET NULL`` -
  the same rule ``booking_requests.appointment_id`` and
  ``communication_logs.appointment_id`` already use, preserving the
  reminder's own history even once the specific appointment is gone.

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
        batch_op.drop_constraint("reminders_customer_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_customer_id_fkey", "customers", ["customer_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.drop_constraint("reminders_vehicle_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_vehicle_id_fkey", "vehicles", ["vehicle_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.drop_constraint("reminders_appointment_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_appointment_id_fkey",
            "appointments",
            ["appointment_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.drop_constraint("reminders_appointment_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_appointment_id_fkey", "appointments", ["appointment_id"], ["id"]
        )
        batch_op.drop_constraint("reminders_vehicle_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key("reminders_vehicle_id_fkey", "vehicles", ["vehicle_id"], ["id"])
        batch_op.drop_constraint("reminders_customer_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "reminders_customer_id_fkey", "customers", ["customer_id"], ["id"]
        )
        batch_op.drop_constraint("reminders_garage_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key("reminders_garage_id_fkey", "garages", ["garage_id"], ["id"])

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("archive_reason")
