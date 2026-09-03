import re

from app.models.garage import Garage

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def slugify(value: str) -> str:
    """A lowercase, hyphen-separated, URL-safe form of `value`."""
    base = _SLUG_TRIM.sub("", _SLUG_STRIP.sub("-", value.lower()))
    return base or "garage"


def slugify_unique(name: str, session) -> str:
    """`slugify(name)`, suffixed with -2, -3, ... until no Garage.slug collides."""
    base = slugify(name)[:120]
    candidate = base
    n = 2
    while session.query(Garage.id).filter_by(slug=candidate).first() is not None:
        suffix = f"-{n}"
        candidate = f"{base[: 120 - len(suffix)]}{suffix}"
        n += 1
    return candidate
