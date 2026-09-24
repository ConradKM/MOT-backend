"""Vehicle details - registration, make and model - as a one-screen switch.

Not a new model: these are ordinary ``BookingFlowField`` rows bound to
``ITEM_REFERENCE`` / ``ITEM_MAKE`` / ``ITEM_MODEL`` in the business's default
workflow, exactly what the automotive preset creates and what the public
booking page already renders and stores (app/booking_flow/answers.py
``tracked_item_kwargs``). This module only makes that configuration easy to
find and hard to get wrong:

- it is idempotent - saving the same choice twice changes nothing;
- make/model can only be asked alongside the registration, because
  ``tracked_item_kwargs`` records make/model only against a registration;
- a business that never turns it on is never asked anything new.

It is also what the AI voice agent reads to know which vehicle details a
service needs (``requirements_for``).
"""

from __future__ import annotations

import uuid

from app.extensions import db
from app.models.booking_flow.field import (
    BINDING_ITEM_MAKE,
    BINDING_ITEM_MODEL,
    BINDING_ITEM_REFERENCE,
    BookingFlowField,
)
from app.models.booking_flow.section import BookingFlowSection

from .resolve import resolve_fields

# Public key -> (binding, label, extra field kwargs)
VEHICLE_FIELDS: dict[str, tuple[str, str, dict]] = {
    "registration": (
        BINDING_ITEM_REFERENCE,
        "Registration number",
        {"max_length": 20, "placeholder": "AB12 CDE"},
    ),
    "make": (BINDING_ITEM_MAKE, "Make", {"max_length": 100, "placeholder": "e.g. Ford"}),
    "model": (BINDING_ITEM_MODEL, "Model", {"max_length": 100, "placeholder": "e.g. Focus"}),
}
SECTION_TITLE = "Vehicle details"
SECTION_DESCRIPTION = "So we know which vehicle you're booking in."

NOT_ASKED = "not_asked"
OPTIONAL = "optional"
REQUIRED = "required"


class VehicleDetailsError(ValueError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


def _default_fields(garage_id: uuid.UUID) -> dict[str, BookingFlowField]:
    """The first bound field per vehicle key in the default workflow."""
    fields = (
        BookingFlowField.query.join(BookingFlowSection)
        .filter(
            BookingFlowField.garage_id == garage_id,
            BookingFlowSection.garage_id == garage_id,
            BookingFlowSection.appointment_type_id.is_(None),
            BookingFlowField.binds_to.in_([b for b, _, _ in VEHICLE_FIELDS.values()]),
        )
        .order_by(BookingFlowSection.order, BookingFlowField.order)
        .all()
    )
    by_binding: dict[str, BookingFlowField] = {}
    for field in fields:
        by_binding.setdefault(field.binds_to, field)
    return {
        key: by_binding[binding]
        for key, (binding, _, _) in VEHICLE_FIELDS.items()
        if binding in by_binding
    }


def get_vehicle_details(garage_id: uuid.UUID) -> dict:
    existing = _default_fields(garage_id)
    overrides = (
        db.session.query(BookingFlowSection.appointment_type_id)
        .filter(
            BookingFlowSection.garage_id == garage_id,
            BookingFlowSection.appointment_type_id.isnot(None),
        )
        .distinct()
        .count()
    )
    return {
        "fields": {
            key: {
                "enabled": key in existing and existing[key].section.is_active,
                "required": bool(key in existing and existing[key].is_required),
            }
            for key in VEHICLE_FIELDS
        },
        # Services with their own workflow don't use the default one, so this
        # switch doesn't reach them - the settings page says so.
        "services_with_own_workflow": overrides,
    }


def update_vehicle_details(garage_id: uuid.UUID, wanted: dict) -> dict:
    """Make the default workflow ask exactly ``wanted``:
    ``{"registration": {"enabled": bool, "required": bool}, "make": ..., "model": ...}``.
    Keys left out are unchanged."""
    current = get_vehicle_details(garage_id)["fields"]
    desired = {key: {**current[key], **(wanted.get(key) or {})} for key in VEHICLE_FIELDS}
    for key, state in desired.items():
        if not state["enabled"]:
            state["required"] = False
    if not desired["registration"]["enabled"] and (
        desired["make"]["enabled"] or desired["model"]["enabled"]
    ):
        raise VehicleDetailsError(
            "Ask for the registration number too - make and model are saved against it.",
            field="registration",
        )

    existing = _default_fields(garage_id)
    section = (
        _vehicle_section(garage_id, existing)
        if any(s["enabled"] for s in desired.values())
        else None
    )

    touched_sections: set[BookingFlowSection] = set()
    for position, (key, (binding, label, extra)) in enumerate(VEHICLE_FIELDS.items()):
        state = desired[key]
        field = existing.get(key)
        if state["enabled"]:
            if field is None:
                assert section is not None
                field = BookingFlowField(
                    garage_id=garage_id,
                    order=position,
                    label=label,
                    field_type="TEXT",
                    binds_to=binding,
                    options=[],
                    **extra,
                )
                section.fields.append(field)
            elif not field.section.is_active:
                field.section.is_active = True
            field.is_required = state["required"]
        elif field is not None:
            touched_sections.add(field.section)
            # Answers keep their own snapshot (BookingFlowAnswer), so removing
            # the question never rewrites what a customer already told us.
            db.session.delete(field)

    db.session.flush()
    for touched in touched_sections:
        if not BookingFlowField.query.filter_by(booking_flow_section_id=touched.id).count():
            # An empty step is a dead page in the customer's booking flow.
            db.session.delete(touched)
    db.session.commit()
    return get_vehicle_details(garage_id)


def _vehicle_section(garage_id: uuid.UUID, existing: dict[str, BookingFlowField]):
    """Where new vehicle fields go: alongside any existing vehicle field,
    else a new first step of the default workflow."""
    for field in existing.values():
        return field.section
    others = BookingFlowSection.query.filter_by(garage_id=garage_id, appointment_type_id=None).all()
    for other in others:
        other.order += 1
    section = BookingFlowSection(
        garage_id=garage_id,
        appointment_type_id=None,
        order=0,
        title=SECTION_TITLE,
        description=SECTION_DESCRIPTION,
        is_active=True,
    )
    db.session.add(section)
    db.session.flush()
    return section


def requirements_for(garage_id: uuid.UUID, appointment_type_id: uuid.UUID | None) -> dict[str, str]:
    """What a booking for this service asks about the vehicle - the same
    resolution the public booking page uses (default workflow or the
    service's own override)."""
    out = dict.fromkeys(VEHICLE_FIELDS, NOT_ASKED)
    binding_to_key = {binding: key for key, (binding, _, _) in VEHICLE_FIELDS.items()}
    for field in resolve_fields(garage_id, appointment_type_id):
        key = binding_to_key.get(field.binds_to or "")
        if key and out[key] != REQUIRED:
            out[key] = REQUIRED if field.is_required else OPTIONAL
    return out
