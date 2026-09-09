"""add communication_logs.initiated_by_employee_id (browser outbound calls)

Revision ID: 5b1e9d4c7a20
Revises: 3f9a1c7b2e84
Create Date: 2026-09-09 16:40:00.000000

Records which staff member placed a browser (Voice SDK) outbound call.
Nullable - inbound calls, WhatsApp and automation rows leave it null.
"""

import sqlalchemy as sa
from alembic import op

revision = "5b1e9d4c7a20"
down_revision = "3f9a1c7b2e84"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "communication_logs",
        sa.Column("initiated_by_employee_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f("ix_communication_logs_initiated_by_employee_id"),
        "communication_logs",
        ["initiated_by_employee_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_communication_logs_initiated_by_employee_id_employees"),
        "communication_logs",
        "employees",
        ["initiated_by_employee_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        op.f("fk_communication_logs_initiated_by_employee_id_employees"),
        "communication_logs",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_communication_logs_initiated_by_employee_id"),
        table_name="communication_logs",
    )
    op.drop_column("communication_logs", "initiated_by_employee_id")
