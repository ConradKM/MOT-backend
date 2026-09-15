"""Starting workflows for a newly onboarded business.

A business should not face an empty booking form on day one, but the platform
also must not hard-code one industry's questions for everybody - that was the
original problem. A preset is the compromise: ordinary rows in
``booking_flow_sections`` / ``booking_flow_fields``, seeded once at onboarding
and fully editable (and deletable) afterwards. Nothing reads a preset again
after it has been applied.

The built-in "Your details" section is *not* here. It is not configuration -
the platform needs name, email and mobile to create the account, send the
confirmation and issue a booking reference - so it is rendered by the booking
page directly and can never be removed.
"""

from __future__ import annotations

import uuid

from app.extensions import db
from app.models.booking_flow.field import (
    BINDING_ITEM_MAKE,
    BINDING_ITEM_MODEL,
    BINDING_ITEM_REFERENCE,
    BINDING_ITEM_USAGE,
    BINDING_ITEM_YEAR,
)
from app.models.booking_flow.section import BookingFlowSection

# A preset is (section title, section description, [field, ...]) where a field
# is a dict of BookingFlowField kwargs. Deliberately plain data: a preset has
# no behaviour of its own and must stay trivially readable by whoever is
# adding the next one.
PRESETS: dict[str, list[tuple[str, str | None, list[dict]]]] = {
    # For a business that books in a vehicle. The bindings are what keep the
    # existing vehicle records - and the MOT reminders built on them - working.
    "automotive": [
        (
            "Vehicle details",
            "So we know what we're working on.",
            [
                {
                    "label": "Registration number",
                    "field_type": "TEXT",
                    "is_required": True,
                    "max_length": 20,
                    "placeholder": "AB12 CDE",
                    "binds_to": BINDING_ITEM_REFERENCE,
                },
                {"label": "Make", "field_type": "TEXT", "binds_to": BINDING_ITEM_MAKE},
                {"label": "Model", "field_type": "TEXT", "binds_to": BINDING_ITEM_MODEL},
                {
                    "label": "Year",
                    "field_type": "NUMBER",
                    "min_value": 1900,
                    "max_value": 2100,
                    "binds_to": BINDING_ITEM_YEAR,
                },
                {
                    "label": "Current mileage",
                    "field_type": "NUMBER",
                    "min_value": 0,
                    "binds_to": BINDING_ITEM_USAGE,
                },
            ],
        ),
        (
            "Anything else",
            "Anything you'd like us to know before your visit.",
            [{"label": "Notes", "field_type": "TEXTAREA", "max_length": 2000}],
        ),
    ],
    # For a business booking a person's time - salon, clinic, studio, trades.
    # Nothing is tracked as a record, so nothing binds.
    "appointments": [
        (
            "About your appointment",
            "A few details so we can prepare.",
            [
                {
                    "label": "Have you visited us before?",
                    "field_type": "SELECT",
                    "is_required": True,
                    "options": ["First visit", "I've been before"],
                },
                {
                    "label": "Anything we should know?",
                    "field_type": "TEXTAREA",
                    "help_text": "Allergies, preferences, access requirements - anything at all.",
                    "max_length": 2000,
                },
            ],
        ),
    ],
    # The minimum that still gives a business somewhere to type.
    "generic": [
        (
            "Anything else",
            "Anything you'd like us to know before your visit.",
            [{"label": "Notes", "field_type": "TEXTAREA", "max_length": 2000}],
        ),
    ],
}

DEFAULT_PRESET = "generic"


def apply_preset(garage_id: uuid.UUID, preset: str, session=None) -> list[BookingFlowSection]:
    """Seed ``garage_id``'s default workflow from ``preset``.

    Raises KeyError for an unknown preset name - callers validate first; this
    is the last line of defence, not the user-facing check.
    """
    session = session or db.session
    spec = PRESETS[preset]

    sections = []
    for order, (title, description, fields) in enumerate(spec):
        section = BookingFlowSection(
            garage_id=garage_id,
            appointment_type_id=None,
            order=order,
            title=title,
            description=description,
        )
        session.add(section)
        session.flush()

        for field_order, field_kwargs in enumerate(fields):
            section.fields.append(_build_field(garage_id, order=field_order, **field_kwargs))

        sections.append(section)

    session.flush()
    return sections


def _build_field(garage_id: uuid.UUID, **kwargs):
    from app.models.booking_flow.field import BookingFlowField

    kwargs.setdefault("options", [])
    return BookingFlowField(garage_id=garage_id, **kwargs)
