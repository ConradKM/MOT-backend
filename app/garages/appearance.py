"""Booking-page colours: a business's page background + page text colour.

Owner-editable (``PUT /api/garage/booking-appearance``) and read by every
customer-facing page of that business. Deliberately just the two colours -
cards, form fields, buttons, the Stripe card form and the CAPTCHA keep their
own look (see MOT-frontend ``BookingThemeScope``), so only the area behind the
cards and the text sitting directly on it change.

Two rules make the pair safe to hand to a browser:

* **Strict ``#rrggbb`` only.** No named colours, ``rgb()``, alpha or any other
  CSS - the value is only ever written into a CSS custom property, and this is
  the injection guard for that.
* **Validated as a pair.** Either both are set or both are NULL (NULL = the
  default look), and the text must contrast with the background at
  WCAG 2.x normal-text AA (>= 4.5:1). The contrast maths is mirrored in
  MOT-frontend ``src/lib/contrast.ts``; both test suites run the same vector
  table (``tests/fixtures/contrast_vectors.json``) so they cannot drift.
"""

from __future__ import annotations

import re

# `\Z`, not `$`: `$` also matches before a trailing newline.
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")

# WCAG 2.x contrast for normal-size text, level AA.
MIN_CONTRAST_RATIO = 4.5


class BookingAppearanceError(ValueError):
    """The submitted colour pair is not acceptable; the message says why."""


def is_hex_color(value: object) -> bool:
    return isinstance(value, str) and HEX_COLOR_RE.match(value) is not None


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour (0 = black, 1 = white)."""
    if not is_hex_color(hex_color):
        raise BookingAppearanceError(f"{hex_color!r} is not a #RRGGBB colour.")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours, 1.0 - 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def normalise_pair(background: str | None, text: str | None) -> tuple[str | None, str | None]:
    """Validate a submitted pair; return it lowercased, or ``(None, None)``.

    Raises :class:`BookingAppearanceError` (message fit for a 422) when only
    one colour is set, either is not ``#RRGGBB``, or the contrast is too low.
    """
    if background is None and text is None:
        return None, None
    if background is None or text is None:
        raise BookingAppearanceError(
            "Set both the background and the text colour, or clear both to use the default."
        )
    for label, value in (("Background", background), ("Text", text)):
        if not is_hex_color(value):
            raise BookingAppearanceError(
                f"{label} colour must be a 6-digit hex colour like #1a2b3c."
            )
    ratio = contrast_ratio(background, text)
    if ratio < MIN_CONTRAST_RATIO:
        raise BookingAppearanceError(
            f"Text and background contrast is {ratio:.2f}:1; it needs to be at least "
            f"{MIN_CONTRAST_RATIO}:1 so customers can read it."
        )
    return background.lower(), text.lower()


def public_colors(garage) -> tuple[str | None, str | None]:
    """The stored pair as safe to publish, or ``(None, None)``.

    Defensive: a value that slipped past validation (a manual DB edit, or a
    stricter rule introduced later) degrades to the default look rather than
    reaching a customer's browser.
    """
    try:
        return normalise_pair(garage.booking_background_color, garage.booking_text_color)
    except BookingAppearanceError:
        return None, None
