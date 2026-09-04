"""Platform-side edit of a tenant's business details.

Garage users see their details read-only (``GET /api/garage``). Changing them
is a developer operation, done here and exposed through
``flask update-garage-details`` - part of the same developer-controlled tenant
configuration as onboarding.

Only ``name``, ``email``, ``phone``, ``address``, ``postcode`` and ``website``
are editable. ``id``, ``slug`` and ``layout_variant`` are never touched here -
the slug is immutable and the rest are platform identity, not "details".
"""

from __future__ import annotations

from app.extensions import db
from app.models.garage import Garage

EDITABLE_FIELDS = ("name", "email", "phone", "address", "postcode", "website")


class GarageNotFoundError(ValueError):
    """No garage matched the given slug or id."""


def resolve_garage(identifier: str, session=None) -> Garage:
    """Find a garage by slug or UUID string. Raises
    :class:`GarageNotFoundError` if nothing matches."""
    session = session or db.session

    garage = Garage.query.filter_by(slug=identifier).first()
    if garage is None:
        try:
            import uuid

            garage = session.get(Garage, uuid.UUID(identifier))
        except (ValueError, TypeError):
            garage = None

    if garage is None:
        raise GarageNotFoundError(f"No garage with slug or id {identifier!r}.")
    return garage


def update_garage_details(
    garage: Garage, *, session=None, commit: bool = True, **fields
) -> Garage:
    """Apply the given business-detail fields to one garage (that tenant only).

    Unknown keys and ``None``-only omissions are ignored; an empty string is a
    real value (clears the field).
    """
    session = session or db.session

    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(
            f"Not editable: {sorted(unknown)}. Allowed: {list(EDITABLE_FIELDS)}."
        )

    applied = {}
    for key in EDITABLE_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(garage, key, fields[key])
            applied[key] = fields[key]

    if not applied:
        raise ValueError("Nothing to update - pass at least one detail field.")

    if commit:
        session.commit()
    else:
        session.flush()
    return garage
