"""Business logo persistence + customer-facing rejection reason

Two independent, purely additive changes bundled together because they land
from the same release-blocker fix (Revive N Drive onboarding):

* ``garages`` gains ``logo_storage_key`` / ``logo_content_type`` /
  ``logo_original_filename`` / ``logo_uploaded_at`` - the object-storage
  reference for a business's logo (see app/garages/logo.py). All nullable;
  every existing garage simply has no logo until one is uploaded.
* ``booking_requests`` gains ``customer_rejection_reason`` - the
  customer-facing decline reason, deliberately separate from the existing
  staff-internal ``staff_notes`` column (see app/models/booking_request.py).
  Nullable; existing rejected requests just have none.

Revision ID: a1f3c9d02b77
Revises: 8b41c7d0e5a2
Create Date: 2026-09-11 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1f3c9d02b77"
down_revision = "8b41c7d0e5a2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("logo_storage_key", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("logo_content_type", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column("logo_original_filename", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("logo_uploaded_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("customer_rejection_reason", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.drop_column("customer_rejection_reason")

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("logo_uploaded_at")
        batch_op.drop_column("logo_original_filename")
        batch_op.drop_column("logo_content_type")
        batch_op.drop_column("logo_storage_key")
