"""Short, customer-facing reference codes for booking requests.

Unlike a garage's slug (app/garages/slug.py), a booking reference has no name
to derive from - it exists purely so a customer can quote a short code over
the phone, or type it into the customer login form, instead of a UUID.
"""

import secrets

# Same unambiguous alphabet as garage slugs (app/garages/slug.py) - no
# 0/O/1/I/L - so a reference read aloud or copied by hand stays intact.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PREFIX = "BK"
RANDOM_PART_LENGTH = 7


def random_reference(length: int = RANDOM_PART_LENGTH) -> str:
    """A "BK"-prefixed, cryptographically random reference, e.g. "BK7F3K9Q2"."""
    return _PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(length))


def unique_booking_reference(session, length: int = RANDOM_PART_LENGTH) -> str:
    """``random_reference()`` regenerated until it doesn't collide with an
    existing ``BookingRequest.booking_reference``.

    A 7-character random part over a 32-symbol alphabet is ~3.4e10
    possibilities, so the retry loop effectively never runs twice - it's
    belt-and-braces against the birthday case, not a counter (mirrors
    app/garages/slug.py::slugify_unique).
    """
    from app.models.booking_request import BookingRequest

    while True:
        candidate = random_reference(length)
        exists = session.query(BookingRequest.id).filter_by(booking_reference=candidate).first()
        if exists is None:
            return candidate
