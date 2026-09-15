"""Service groups, explicit display ordering, and service images.

Three related additions, all in service of letting a business present its own
menu rather than a flat alphabetical list:

* ``appointment_type_groups`` - an optional navigational grouping. Grouping is
  opt-in and stays empty for a business with a short menu; the FK on the child
  side is deliberately ``SET NULL`` so deleting a group ungroups its services
  instead of deleting them and their booking history along with it.

* ``order`` on both tables - a business sells in its own order, which is rarely
  alphabetical. Backfilled to 0 for every existing row, which preserves today's
  behaviour exactly: the list endpoints order by ``(order, name)``, so a
  business that has never reordered anything still gets the alphabetical
  listing it has always had.

* ``image_*`` on both tables, and ``garages.booking_display_mode`` - see
  app/models/appointments/appointment_type_group.py::DISPLAY_MODES. GRID is
  image-led and suits a business selling a *look*; LIST is information-led.
  The default is LIST because it renders correctly with no images configured,
  which is the state every business starts in.

The image columns are references, never blobs - the bytes live in object
storage and only ever arrive through app/storage/images.py's presigned flow.

Revision ID: 6752d8b37573
Revises: 9b4e7f21c6a3
Create Date: 2026-09-15 10:58:08.267182

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "6752d8b37573"
down_revision = "9b4e7f21c6a3"
branch_labels = None
depends_on = None

# Named explicitly rather than left to Alembic's None: an unnamed constraint
# cannot be dropped by name in downgrade().
_GROUP_FK = "fk_garage_appointment_types_group_id"


def upgrade():
    op.create_table(
        "appointment_type_groups",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        # NULL inherits garages.booking_display_mode rather than freezing a
        # copy of it, so changing the business default moves every group that
        # never expressed a preference of its own.
        sa.Column("display_mode", sa.String(length=10), nullable=True),
        sa.Column("image_storage_key", sa.String(length=500), nullable=True),
        sa.Column("image_content_type", sa.String(length=100), nullable=True),
        sa.Column("image_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("appointment_type_groups", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_appointment_type_groups_garage_id"), ["garage_id"], unique=False
        )
        # The ORM default covers new rows; the server default existed only to
        # backfill the table being created above, and is dropped for the same
        # reason as on garage_appointment_types below.
        batch_op.alter_column("order", server_default=None)

    with op.batch_alter_table("garage_appointment_types", schema=None) as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.Uuid(), nullable=True))
        # server_default backfills every existing row to 0 so the NOT NULL can
        # be applied at all; dropped immediately afterwards so the ORM default
        # (Python-side) stays the single source of truth for new rows.
        batch_op.add_column(sa.Column("order", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("image_storage_key", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("image_content_type", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column("image_uploaded_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.alter_column("order", server_default=None)
        batch_op.create_index(
            batch_op.f("ix_garage_appointment_types_group_id"), ["group_id"], unique=False
        )
        batch_op.create_foreign_key(
            _GROUP_FK,
            "appointment_type_groups",
            ["group_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("garages", schema=None) as batch_op:
        # Keeps its server_default: unlike `order` this is a genuine column
        # default that existing and future rows should both fall back to, and
        # the model declares the same one.
        batch_op.add_column(
            sa.Column(
                "booking_display_mode",
                sa.String(length=10),
                nullable=False,
                server_default="LIST",
            )
        )


def downgrade():
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("booking_display_mode")

    with op.batch_alter_table("garage_appointment_types", schema=None) as batch_op:
        batch_op.drop_constraint(_GROUP_FK, type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_garage_appointment_types_group_id"))
        batch_op.drop_column("image_uploaded_at")
        batch_op.drop_column("image_content_type")
        batch_op.drop_column("image_storage_key")
        batch_op.drop_column("order")
        batch_op.drop_column("group_id")

    with op.batch_alter_table("appointment_type_groups", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_appointment_type_groups_garage_id"))

    op.drop_table("appointment_type_groups")
