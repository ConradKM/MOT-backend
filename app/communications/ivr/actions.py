"""The phone-menu action registry - what pressing a digit can do.

Each action is one :class:`IvrAction` entry. It says whether it needs a
transfer ``target``, whether it may be a fallback (it must terminate the
menu), and whether it is available right now for a given business. Adding a
new action later (voicemail, a different AI route, SMS a booking link...) is a
new entry here plus its TwiML in app/communications/ivr/twiml.py - no schema
change, no rewrite of the voice path.

Validation is the security boundary for transfer destinations: every
``target`` is a business-configured UK number, normalised to E.164, and never
premium- or personal-rate. Nothing a caller or the AI says can reach
``target`` - it is only ever read from this business's saved settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from flask import current_app

from app.phone import InvalidPhoneNumberError, normalize_uk_phone

AI_BOOKING = "AI_BOOKING"
AI_FAQ = "AI_FAQ"
HUMAN_TRANSFER = "HUMAN_TRANSFER"
REPEAT_MENU = "REPEAT_MENU"

AI_ACTIONS = frozenset({AI_BOOKING, AI_FAQ})

DTMF_DIGITS = tuple("0123456789")
MAX_LABEL_LENGTH = 60
MAX_PROMPT_LENGTH = 200
MAX_GREETING_LENGTH = 500
MIN_ATTEMPTS, MAX_ATTEMPTS = 1, 5

# UK number ranges a business must never be able to make CoMaz's Twilio
# account dial on every call: premium rate (09), personal numbering (070),
# higher-rate service numbers (087), and directory enquiries (118). All are
# classic toll-fraud destinations.
_BLOCKED_NATIONAL_PREFIXES = ("9", "70", "87", "118")


class IvrValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


def ai_voice_available() -> bool:
    """Whether this deployment can put a caller through to the AI agent at
    all - the menu never offers a dead end, it falls back instead."""
    from app.ai_voice.config import openai_configured, openai_voice_enabled

    return bool(
        openai_voice_enabled()
        and openai_configured()
        and current_app.config.get("OPENAI_PROJECT_ID")
    )


@dataclass(frozen=True)
class IvrAction:
    key: str
    label: str
    # "none" - no target allowed; "optional" - per-option target, falling
    # back to the business-wide transfer destination.
    target: str
    # May be used as the menu's fallback_action (must not loop back).
    terminal: bool
    available: Callable[[], bool]


def _always() -> bool:
    return True


ACTIONS: dict[str, IvrAction] = {
    AI_BOOKING: IvrAction(AI_BOOKING, "AI booking assistant", "none", True, ai_voice_available),
    AI_FAQ: IvrAction(AI_FAQ, "AI questions assistant", "none", True, ai_voice_available),
    HUMAN_TRANSFER: IvrAction(HUMAN_TRANSFER, "Transfer to a person", "optional", True, _always),
    REPEAT_MENU: IvrAction(REPEAT_MENU, "Repeat the menu", "none", False, _always),
}

FALLBACK_ACTIONS = tuple(key for key, action in ACTIONS.items() if action.terminal)


def normalise_transfer_number(raw: str, *, field: str = "target") -> str:
    """A safe UK E.164 transfer destination, or IvrValidationError."""
    try:
        e164 = normalize_uk_phone(raw)
    except InvalidPhoneNumberError as exc:
        raise IvrValidationError(str(exc), field=field) from exc
    # normalize_uk_phone parses with a GB default but accepts any valid
    # international number ("+1 212 ..."); transfers stay UK-only.
    if not e164.startswith("+44"):
        raise IvrValidationError("Transfers can only go to a UK phone number.", field=field)
    national = e164[len("+44") :]
    if any(national.startswith(prefix) for prefix in _BLOCKED_NATIONAL_PREFIXES):
        raise IvrValidationError(
            "Premium-rate, personal and directory numbers can't be used as a transfer destination.",
            field=field,
        )
    return e164


def validate_option(raw: dict, index: int) -> dict:
    """One menu option, normalised. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        raise IvrValidationError("Each option must be an object.", field=f"options.{index}")
    digit = str(raw.get("digit", "")).strip()
    if digit not in DTMF_DIGITS:
        raise IvrValidationError(
            "Choose a keypad digit from 0 to 9.", field=f"options.{index}.digit"
        )
    action_key = str(raw.get("action", "")).strip().upper()
    action = ACTIONS.get(action_key)
    if action is None:
        raise IvrValidationError("Choose what this option does.", field=f"options.{index}.action")
    label = str(raw.get("label") or "").strip()
    if not label or len(label) > MAX_LABEL_LENGTH:
        raise IvrValidationError(
            f"Give this option a name of up to {MAX_LABEL_LENGTH} characters.",
            field=f"options.{index}.label",
        )
    prompt = str(raw.get("prompt") or "").strip() or None
    if prompt is not None and len(prompt) > MAX_PROMPT_LENGTH:
        raise IvrValidationError(
            f"Keep the spoken prompt under {MAX_PROMPT_LENGTH} characters.",
            field=f"options.{index}.prompt",
        )
    raw_target = str(raw.get("target") or "").strip()
    target = None
    if raw_target:
        if action.target == "none":
            raise IvrValidationError(
                "Only a transfer option can have a phone number.",
                field=f"options.{index}.target",
            )
        target = normalise_transfer_number(raw_target, field=f"options.{index}.target")
    return {
        "digit": digit,
        "label": label,
        "prompt": prompt,
        "action": action_key,
        "target": target,
    }


def validate_options(raw_options) -> list[dict]:
    if not isinstance(raw_options, list):
        raise IvrValidationError("options must be a list.", field="options")
    options = [validate_option(raw, i) for i, raw in enumerate(raw_options)]
    digits = [o["digit"] for o in options]
    if len(set(digits)) != len(digits):
        raise IvrValidationError("Each keypad digit can only be used once.", field="options")
    return sorted(options, key=lambda o: o["digit"])
