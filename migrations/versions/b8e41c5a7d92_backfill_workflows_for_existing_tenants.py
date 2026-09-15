"""Give every existing business the booking form it already had.

Without this, deploying the configurable booking form is a silent data-loss
bug for every business already using the product.

The previous migration made ``vehicle_registration`` nullable and moved what
the form asks into ``booking_flow_sections`` / ``booking_flow_fields``. An
existing business has no rows in those tables, so the booking page would
resolve an empty workflow and ask for nothing but a name, email and mobile -
and, because the column is now nullable, the submission would *succeed*. A
garage would start taking bookings with no registration, no vehicle record,
and therefore no MOT reminder, with nothing to indicate anything had changed.

So this seeds the automotive preset as the default workflow for every business
that has none, reproducing exactly what the old hard-coded form collected:
registration required, make/model/year/mileage optional, and a free-text note.
The bindings are the important part - they are what keeps vehicle records (and
the reminders built on them) being created as before.

Deliberately skips any business that already has a default workflow, so it is
safe to re-run and never overwrites configuration someone has built.

It also deliberately does **not** import app/booking_flow/presets.py. A
migration has to describe the schema and data as they are at this point in
history; importing live application code would make an old migration change
meaning the next time that file is edited.

Revision ID: b8e41c5a7d92
Revises: 344d8f129cc9
Create Date: 2026-09-15 12:40:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8e41c5a7d92"
down_revision = "344d8f129cc9"
branch_labels = None
depends_on = None

# A frozen copy of the "automotive" preset as it stood when this migration was
# written - see the module docstring for why it is copied rather than imported.
# (order, label, field_type, is_required, placeholder, min_value, max_value,
#  max_length, binds_to)
_VEHICLE_FIELDS = [
    (0, "Registration number", "TEXT", True, "AB12 CDE", None, None, 20, "ITEM_REFERENCE"),
    (1, "Make", "TEXT", False, None, None, None, None, "ITEM_MAKE"),
    (2, "Model", "TEXT", False, None, None, None, None, "ITEM_MODEL"),
    (3, "Year", "NUMBER", False, None, 1900, 2100, None, "ITEM_YEAR"),
    (4, "Current mileage", "NUMBER", False, None, 0, None, None, "ITEM_USAGE"),
]

_NOTES_FIELDS = [
    (0, "Notes", "TEXTAREA", False, None, None, None, 2000, None),
]

_SECTIONS = [
    (0, "Vehicle details", "So we know what we're working on.", _VEHICLE_FIELDS),
    (
        1,
        "Anything else",
        "Anything you'd like us to know before your visit.",
        _NOTES_FIELDS,
    ),
]


def upgrade():
    conn = op.get_bind()

    garage_ids = [
        row[0]
        for row in conn.execute(
            sa.text(
                """
                SELECT g.id
                FROM garages g
                WHERE NOT EXISTS (
                    SELECT 1 FROM booking_flow_sections s
                    WHERE s.garage_id = g.id AND s.appointment_type_id IS NULL
                )
                """
            )
        )
    ]

    for garage_id in garage_ids:
        for order, title, description, fields in _SECTIONS:
            section_id = conn.execute(
                sa.text(
                    """
                    INSERT INTO booking_flow_sections
                        (id, garage_id, appointment_type_id, "order", title,
                         description, is_active, created_at, updated_at)
                    VALUES
                        (gen_random_uuid(), :garage_id, NULL, :order, :title,
                         :description, true, now(), now())
                    RETURNING id
                    """
                ),
                {
                    "garage_id": garage_id,
                    "order": order,
                    "title": title,
                    "description": description,
                },
            ).scalar_one()

            for (
                field_order,
                label,
                field_type,
                is_required,
                placeholder,
                min_value,
                max_value,
                max_length,
                binds_to,
            ) in fields:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO booking_flow_fields
                            (id, garage_id, booking_flow_section_id, "order", label,
                             help_text, placeholder, field_type, is_required, options,
                             min_value, max_value, max_length, binds_to,
                             created_at, updated_at)
                        VALUES
                            (gen_random_uuid(), :garage_id, :section_id, :order, :label,
                             NULL, :placeholder, :field_type, :is_required, '{}',
                             :min_value, :max_value, :max_length, :binds_to,
                             now(), now())
                        """
                    ),
                    {
                        "garage_id": garage_id,
                        "section_id": section_id,
                        "order": field_order,
                        "label": label,
                        "placeholder": placeholder,
                        "field_type": field_type,
                        "is_required": is_required,
                        "min_value": min_value,
                        "max_value": max_value,
                        "max_length": max_length,
                        "binds_to": binds_to,
                    },
                )


def downgrade():
    # Deliberately a no-op. This migration only ever *adds* rows that a
    # business is then free to edit, so by the time anyone downgrades there is
    # no way to tell a seeded section from one they have since rewritten -
    # and deleting the wrong one would throw away real configuration. The
    # previous migration's downgrade drops these tables entirely anyway.
    pass
