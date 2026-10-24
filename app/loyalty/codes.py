"""Discount code text: generation and normalisation.

Every lookup compares the *normalised* form - upper case, with surrounding
whitespace, inner spaces and dashes removed - so "loyal-7kq2 m9xa", "LOYAL 7KQ2M9XA"
and "LOYAL-7KQ2M9XA" are the same code wherever a customer or staff member
types it.
"""

from __future__ import annotations

import re
import secrets

# No 0/O or 1/I: a code read out over the phone or copied from a printed
# receipt should never be ambiguous.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GENERATED_LENGTH = 8
LOYALTY_PREFIX = "LOYAL-"

MIN_CODE_LENGTH = 3
MAX_CODE_LENGTH = 32
_VALID_NORMALISED = re.compile(r"^[A-Z0-9]+$")


def normalize_code(raw: str | None) -> str:
    """The comparison form of ``raw``; empty for anything unusable."""
    if not raw:
        return ""
    return re.sub(r"[\s\-]+", "", raw).upper()


def display_code(raw: str) -> str:
    """How a hand-typed code is stored and shown: trimmed, upper case, inner
    whitespace removed, dashes kept (so ``summer-10`` stays readable)."""
    return re.sub(r"\s+", "", raw).upper()


def is_valid_code(normalised: str) -> bool:
    return (
        MIN_CODE_LENGTH <= len(normalised) <= MAX_CODE_LENGTH
        and _VALID_NORMALISED.match(normalised) is not None
    )


def generate_code(prefix: str = "") -> str:
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(GENERATED_LENGTH))
    return f"{prefix}{body}"
