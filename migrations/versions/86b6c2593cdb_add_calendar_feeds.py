"""Add calendar subscription feeds (app/calendar_export)

Revision ID: 86b6c2593cdb
Revises: 3a7c1e9d5b20
Create Date: 2026-10-10 23:59:20.235509

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "86b6c2593cdb"
down_revision = "3a7c1e9d5b20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_feeds",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_employee_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("all_appointment_types", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("days_back", sa.Integer(), nullable=False),
        sa.Column("days_forward", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("calendar_feeds", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_calendar_feeds_created_by_employee_id"),
            ["created_by_employee_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_calendar_feeds_garage_id"), ["garage_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_calendar_feeds_token_hash"), ["token_hash"], unique=True
        )

    op.create_table(
        "calendar_feed_appointment_types",
        sa.Column("calendar_feed_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_type_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_type_id"], ["garage_appointment_types.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["calendar_feed_id"], ["calendar_feeds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("calendar_feed_id", "appointment_type_id"),
    )
    op.create_table(
        "calendar_feed_employees",
        sa.Column("calendar_feed_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["calendar_feed_id"], ["calendar_feeds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("calendar_feed_id", "employee_id"),
    )


def downgrade():
    op.drop_table("calendar_feed_employees")
    op.drop_table("calendar_feed_appointment_types")
    with op.batch_alter_table("calendar_feeds", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_calendar_feeds_token_hash"))
        batch_op.drop_index(batch_op.f("ix_calendar_feeds_garage_id"))
        batch_op.drop_index(batch_op.f("ix_calendar_feeds_created_by_employee_id"))

    op.drop_table("calendar_feeds")
