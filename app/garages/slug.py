"""Public garage slug generation.

A garage's ``slug`` is the identifier that appears in unauthenticated URLs
(``/api/public/<slug>/booking-requests``). It is:

* **derived from the business name** for readability, then
* **suffixed with a short random token** so it can't be guessed from the name,
  so two garages called "City Motors" never collide, and so the public URL is
  decoupled from the display name, and
* **immutable** once the garage is onboarded - nothing in the API accepts a
  ``slug`` or lets a garage user change it. Renaming the business changes
  ``Garage.name`` only.

See ``docs/GARAGE_ONBOARDING.md`` for the full rationale.
"""

import re
import secrets

from app.models.garage import Garage

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")

# Unambiguous lowercase alphanumerics - no 0/o/1/l/i so a slug read aloud or
# copied by hand stays intact.
_SUFFIX_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
SLUG_RANDOM_SUFFIX_LENGTH = 6

# Total column width is 120 (see Garage.slug); keep room for "-" + suffix.
_MAX_SLUG_LENGTH = 120
_MAX_STEM_LENGTH = _MAX_SLUG_LENGTH - 1 - SLUG_RANDOM_SUFFIX_LENGTH


def slugify(value: str) -> str:
    """A lowercase, hyphen-separated, URL-safe form of ``value``."""
    base = _SLUG_TRIM.sub("", _SLUG_STRIP.sub("-", value.lower()))
    return base or "garage"


def random_suffix(length: int = SLUG_RANDOM_SUFFIX_LENGTH) -> str:
    """A cryptographically random lowercase-alphanumeric token."""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(length))


def slugify_unique(name: str, session) -> str:
    """``slugify(name)`` plus a random suffix, regenerated until it doesn't
    collide with an existing ``Garage.slug``.

    A 6-character suffix over a 30-symbol alphabet is ~7.3e8 possibilities, so
    the retry loop effectively never runs twice - it's belt-and-braces against
    the birthday case, not a counter.
    """
    stem = slugify(name)[:_MAX_STEM_LENGTH]

    while True:
        candidate = f"{stem}-{random_suffix()}"
        if session.query(Garage.id).filter_by(slug=candidate).first() is None:
            return candidate
