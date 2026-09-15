"""Which sections and fields apply to a given booking.

Two-level resolution, in one place so the public payload, the submission
validator and the staff review screen can never disagree about what a
customer was asked:

* a business-default workflow (sections with ``appointment_type_id`` NULL);
* an optional per-service override (sections naming that service).

An override **replaces** the default rather than appending to it. Appending
would make it impossible to *drop* a default section for one service, and
"replace" can express both - a service that wants the defaults plus one extra
section copies the defaults into its own override. That choice is worth being
explicit about because it is the one thing about this model that can surprise
a business.
"""

from __future__ import annotations

import uuid

from app.models.booking_flow.section import BookingFlowSection


def resolve_sections(
    garage_id: uuid.UUID, appointment_type_id: uuid.UUID | None
) -> list[BookingFlowSection]:
    """The active sections a customer booking ``appointment_type_id`` sees, in
    display order. Falls back to the business default when the service has no
    override of its own (or when no service was chosen at all)."""
    if appointment_type_id is not None:
        override = _active_sections(
            BookingFlowSection.query.filter_by(
                garage_id=garage_id, appointment_type_id=appointment_type_id
            )
        )
        if override:
            return override

    return _active_sections(
        BookingFlowSection.query.filter_by(garage_id=garage_id, appointment_type_id=None)
    )


def _active_sections(query) -> list[BookingFlowSection]:
    return list(
        query.filter(BookingFlowSection.is_active.is_(True))
        .order_by(BookingFlowSection.order, BookingFlowSection.title)
        .all()
    )


def resolve_fields(garage_id: uuid.UUID, appointment_type_id: uuid.UUID | None) -> list:
    """Every field across the resolved sections, flattened in display order.

    The flat view is what submission validates against; the nested view
    (sections carrying their fields) is what the booking page renders.
    """
    return [
        field
        for section in resolve_sections(garage_id, appointment_type_id)
        for field in section.fields
    ]
