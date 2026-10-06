"""Add appointment add-ons and their per-booking snapshots

Revision ID: 3a7c1e9d5b20
Revises: 5f242d03154a
Create Date: 2026-09-26 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "3a7c1e9d5b20"
down_revision = "5f242d03154a"
branch_labels = None
depends_on = None


def _applied_columns():
    """Columns shared by both snapshot tables (see AppliedAddOnMixin)."""
    return [
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("add_on_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_delta", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("duration_delta_minutes", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["add_on_id"], ["add_ons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade():
    op.create_table(
        "add_ons",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_type_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("price_delta", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("duration_delta_minutes", sa.Integer(), nullable=False),
        sa.Column("max_quantity", sa.Integer(), nullable=False),
        sa.Column("exclusivity_group", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_quantity >= 1 AND max_quantity <= 99", name="ck_add_ons_max_quantity_range"
        ),
        sa.ForeignKeyConstraint(
            ["appointment_type_id"], ["garage_appointment_types.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("add_ons", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_add_ons_appointment_type_id"), ["appointment_type_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_add_ons_garage_id"), ["garage_id"], unique=False)

    op.create_table(
        "appointment_add_ons",
        sa.Column("appointment_id", sa.Uuid(), nullable=False),
        *_applied_columns(),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
    )
    with op.batch_alter_table("appointment_add_ons", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_appointment_add_ons_add_on_id"), ["add_on_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_appointment_add_ons_appointment_id"), ["appointment_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_appointment_add_ons_garage_id"), ["garage_id"], unique=False
        )

    op.create_table(
        "booking_request_add_ons",
        sa.Column("booking_request_id", sa.Uuid(), nullable=False),
        *_applied_columns(),
        sa.ForeignKeyConstraint(
            ["booking_request_id"], ["booking_requests.id"], ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("booking_request_add_ons", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_request_add_ons_add_on_id"), ["add_on_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_booking_request_add_ons_booking_request_id"),
            ["booking_request_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_booking_request_add_ons_garage_id"), ["garage_id"], unique=False
        )


def downgrade():
    op.drop_table("booking_request_add_ons")
    op.drop_table("appointment_add_ons")
    op.drop_table("add_ons")
