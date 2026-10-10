"""Add business Terms & Conditions and per-booking acceptance

A business's current terms live on ``garages`` (text + version), every
wording ever saved in the new immutable ``garage_terms_versions`` table, and
each booking request / queue entry records which version was accepted and
when. ``booking_requests.terms_acceptance_token_hash`` backs the texted
acceptance link for WhatsApp/SMS/phone bookings (status AWAITING_TERMS -
a String(20) value, so no column change is needed for it). Every column is
nullable: a business without terms, and every existing row, is unaffected.

Revision ID: f85f6806ad5e
Revises: 3a7c1e9d5b20
Create Date: 2026-10-07 00:10:05.279065

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f85f6806ad5e"
down_revision = "3a7c1e9d5b20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "garage_terms_versions",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by_employee_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("garage_id", "version", name="uq_garage_terms_versions_garage_version"),
    )
    with op.batch_alter_table("garage_terms_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_garage_terms_versions_garage_id"), ["garage_id"], unique=False
        )

    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("terms_version", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("terms_acceptance_token_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_booking_requests_terms_acceptance_token_hash"),
            ["terms_acceptance_token_hash"],
            unique=True,
        )

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("terms_and_conditions", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("terms_version", sa.Integer(), nullable=True))

    with op.batch_alter_table("queue_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("terms_version", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("queue_entries", schema=None) as batch_op:
        batch_op.drop_column("terms_version")
        batch_op.drop_column("terms_accepted_at")

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("terms_version")
        batch_op.drop_column("terms_and_conditions")

    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_requests_terms_acceptance_token_hash"))
        batch_op.drop_column("terms_acceptance_token_hash")
        batch_op.drop_column("terms_version")
        batch_op.drop_column("terms_accepted_at")

    with op.batch_alter_table("garage_terms_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_garage_terms_versions_garage_id"))

    op.drop_table("garage_terms_versions")
