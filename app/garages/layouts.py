"""Tenant layout registry.

Every garage renders the **same shared UI**. A garage may optionally be pinned
to a named layout *variant* - a bundle of presentation-only overrides
(theme tokens, which optional dashboard panels show, ...). The important rule:

    A variant is a DATA entry in LAYOUT_VARIANTS, resolved by key.
    Never `if garage.name == "..."` or `if garage.id == "..."` anywhere.

Business logic stays shared and identical for every tenant; only the values in
these dicts differ. Adding a bespoke layout for one client is a new entry here
(and, if it needs new presentation code, a branch on the *variant key* in the
frontend registry - never on the garage identity).

See ``docs/GARAGE_ONBOARDING.md`` -> "Layout variants".
"""

DEFAULT_LAYOUT = "default"

# key -> presentation config. Consumed by the frontend via GET /api/garage
# (`layout_variant` is echoed back); the values here are advisory metadata the
# API can expose, not switches the backend branches on.
LAYOUT_VARIANTS: dict[str, dict] = {
    "default": {
        "label": "Standard",
        "description": "The shared default layout every garage gets.",
    },
}


def is_known_variant(variant: str | None) -> bool:
    """True if ``variant`` names a registered layout (``None`` is not a name)."""
    return variant is not None and variant in LAYOUT_VARIANTS


def resolve_layout(garage) -> str:
    """The layout key to render for ``garage``.

    Falls back to :data:`DEFAULT_LAYOUT` when the garage has no variant pinned
    or names one that isn't registered (e.g. a variant retired after it was
    assigned).
    """
    variant = getattr(garage, "layout_variant", None)
    return variant if is_known_variant(variant) else DEFAULT_LAYOUT


def validate_layout_variant(variant: str | None) -> None:
    """Guard for onboarding input. ``None`` is allowed (means default); any
    non-null value must be a registered key."""
    if variant is not None and not is_known_variant(variant):
        known = ", ".join(sorted(LAYOUT_VARIANTS))
        raise ValueError(
            f"Unknown layout_variant {variant!r}. Registered variants: {known}."
        )
