"""A booking form each business configures for itself.

The public booking form was hard-coded for one industry - a vehicle section
with a required registration, backed by a NOT NULL column - so a business that
does not book vehicles in could not use the product at all. This replaces that
with configuration:

* ``booking_flow_sections`` / ``booking_flow_fields`` - what a business asks.
  A section with ``appointment_type_id`` NULL belongs to the business's default
  workflow; one naming a service is that service's override, which *replaces*
  the default rather than appending to it (see app/booking_flow/resolve.py for
  why that is the rule that can express both).

* ``booking_request_answers`` - what a customer answered, snapshotted. The
  section title, label and field type are copied at submission rather than read
  live through the FK, so a business that later renames or deletes a field does
  not rewrite what a customer was asked six weeks ago. Same reasoning as
  AppointmentChecklistItem against its template, and as
  BookingRequest.requested_price.

* ``booking_requests.vehicle_registration`` becomes nullable. What is collected
  about the thing being booked in is now an ordinary field with a binding, and
  a business that tracks nothing collects no identifier. Businesses that do
  bind one still populate this and their item records exactly as before, which
  is what keeps the reminders built on them working.

* ``booking_requests.source`` - which channel the request came through. Not
  cosmetic: the conversational channel (WhatsApp/voice) is a fixed state
  machine and cannot ask configured questions, so its requests arrive with no
  answers. Without this, the staff review screen cannot tell "this business
  asks nothing" from "this business asks three things and none were answered".

Revision ID: 344d8f129cc9
Revises: 6752d8b37573
Create Date: 2026-09-15 11:14:27.357345

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "344d8f129cc9"
down_revision = "6752d8b37573"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_flow_sections",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_type_id", sa.Uuid(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_type_id"], ["garage_appointment_types.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("booking_flow_sections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_flow_sections_appointment_type_id"),
            ["appointment_type_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_booking_flow_sections_garage_id"), ["garage_id"], unique=False
        )

    op.create_table(
        "booking_flow_fields",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("booking_flow_section_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("help_text", sa.String(length=500), nullable=True),
        sa.Column("placeholder", sa.String(length=200), nullable=True),
        sa.Column("field_type", sa.String(length=20), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("options", postgresql.ARRAY(sa.String(length=200)), nullable=False),
        sa.Column("min_value", sa.Integer(), nullable=True),
        sa.Column("max_value", sa.Integer(), nullable=True),
        sa.Column("max_length", sa.Integer(), nullable=True),
        sa.Column("binds_to", sa.String(length=30), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_flow_section_id"], ["booking_flow_sections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("booking_flow_fields", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_flow_fields_booking_flow_section_id"),
            ["booking_flow_section_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_booking_flow_fields_garage_id"), ["garage_id"], unique=False
        )

    op.create_table(
        "booking_request_answers",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("booking_request_id", sa.Uuid(), nullable=False),
        sa.Column("booking_flow_field_id", sa.Uuid(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.String(length=200), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("field_type", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_list", postgresql.ARRAY(sa.String(length=200)), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_flow_field_id"], ["booking_flow_fields.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["booking_request_id"], ["booking_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("booking_request_answers", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_request_answers_booking_request_id"),
            ["booking_request_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_booking_request_answers_garage_id"), ["garage_id"], unique=False
        )

    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        # Keeps its server_default: every request that predates this column
        # came through the web form, and WEB stays the correct fallback for
        # any future writer that forgets to set it.
        batch_op.add_column(
            sa.Column("source", sa.String(length=20), nullable=False, server_default="WEB")
        )
        batch_op.alter_column(
            "vehicle_registration", existing_type=sa.VARCHAR(length=20), nullable=True
        )


def downgrade():
    # Restoring NOT NULL would fail outright on any booking taken by a
    # business that tracks no item, and those rows are real customer bookings -
    # deleting them to make a downgrade work would be far worse than the
    # placeholder. A downgrade cannot invent an identifier that was never
    # collected, so it marks them instead of pretending.
    op.execute(
        "UPDATE booking_requests SET vehicle_registration = 'NOT COLLECTED' "
        "WHERE vehicle_registration IS NULL"
    )

    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.alter_column(
            "vehicle_registration", existing_type=sa.VARCHAR(length=20), nullable=False
        )
        batch_op.drop_column("source")

    with op.batch_alter_table("booking_request_answers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_request_answers_garage_id"))
        batch_op.drop_index(batch_op.f("ix_booking_request_answers_booking_request_id"))

    op.drop_table("booking_request_answers")
    with op.batch_alter_table("booking_flow_fields", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_flow_fields_garage_id"))
        batch_op.drop_index(batch_op.f("ix_booking_flow_fields_booking_flow_section_id"))

    op.drop_table("booking_flow_fields")
    with op.batch_alter_table("booking_flow_sections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_flow_sections_garage_id"))
        batch_op.drop_index(batch_op.f("ix_booking_flow_sections_appointment_type_id"))

    op.drop_table("booking_flow_sections")
