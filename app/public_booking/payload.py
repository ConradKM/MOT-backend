"""The one place the public booking payload is assembled.

There are two ways in to the same page: ``/api/public/<slug>`` and
``/api/public/garages/<uuid>`` (the ``/book/:garageId`` entry point the QR
codes and onboarding emails use). They must return the same thing, and the
second one silently fell behind the first the moment groups were added - the
booking page crashed on a missing key, and the tests could not catch it
because both endpoints were mocked.

So neither route builds the payload itself any more.
"""

from __future__ import annotations

from app.garages.logo import logo_public_url
from app.storage.images import image_url


def public_garage_payload(garage) -> dict:
    """Everything a logged-out customer needs to render the booking page."""
    active = sorted(
        (t for t in garage.appointment_types if t.status == "ACTIVE"),
        key=lambda t: (t.order, t.name),
    )
    # Only groups that still have something active in them - an empty group is
    # a configuration leftover, and rendering it would give the customer a
    # heading to click that leads nowhere.
    grouped_ids = {t.group_id for t in active if t.group_id is not None}

    return {
        "id": garage.id,
        "name": garage.name,
        "slug": garage.slug,
        "logo_url": logo_public_url(garage),
        "booking_display_mode": garage.booking_display_mode,
        "appointment_type_groups": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "order": g.order,
                # Resolve NULL-inherits here so no client reimplements it.
                "display_mode": g.display_mode or garage.booking_display_mode,
                "image_url": image_url(g.image_storage_key),
            }
            for g in garage.appointment_type_groups
            if g.id in grouped_ids
        ],
        "appointment_types": active,
    }
