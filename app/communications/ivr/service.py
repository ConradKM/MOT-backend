"""Reading, validating and saving a business's phone menu."""

from __future__ import annotations

from app.extensions import db
from app.models.communications.voice_ivr_settings import GarageVoiceIvrSettings

from . import actions
from .actions import IvrValidationError

DEFAULT_MAX_ATTEMPTS = 2


def get_settings(garage_id) -> GarageVoiceIvrSettings | None:
    """This business's menu row - always scoped by the caller-resolved
    tenant, never by anything in the request."""
    settings: GarageVoiceIvrSettings | None = GarageVoiceIvrSettings.query.filter_by(
        garage_id=garage_id
    ).first()
    return settings


def active_menu(garage) -> GarageVoiceIvrSettings | None:
    """The menu to play for an inbound call, or None to keep the pre-IVR
    behaviour exactly (no row, disabled, or no options)."""
    settings = get_settings(garage.id)
    if settings is None or not settings.enabled or not settings.options:
        return None
    return settings


def platform_transfer_number(garage) -> str | None:
    comm = getattr(garage, "communication_settings", None)
    if comm is None:
        return None
    return comm.voice_escalation_number or comm.voice_fallback_number or None


def transfer_target(
    garage, settings: GarageVoiceIvrSettings | None, option: dict | None = None
) -> str | None:
    """Where a transfer goes: the option's own number, then the menu's
    fallback number, then the menu's first transfer option's number (the
    business's own "speak to us" line), then the platform-set escalation
    number. Only ever business/platform configuration - never caller or AI
    input."""
    if option and option.get("target"):
        return str(option["target"])
    if settings is not None:
        if settings.fallback_target:
            return settings.fallback_target
        for candidate in settings.options or []:
            if candidate.get("action") == actions.HUMAN_TRANSFER and candidate.get("target"):
                return str(candidate["target"])
    return platform_transfer_number(garage)


def option_for_digit(settings: GarageVoiceIvrSettings, digit: str) -> dict | None:
    options: list[dict] = settings.options or []
    for option in options:
        if option.get("digit") == digit:
            return option
    return None


def serialise(garage, settings: GarageVoiceIvrSettings | None) -> dict:
    return {
        "enabled": bool(settings and settings.enabled),
        "greeting": settings.greeting if settings else None,
        "options": list(settings.options or []) if settings else [],
        "fallback_action": settings.fallback_action if settings else actions.HUMAN_TRANSFER,
        "fallback_target": settings.fallback_target if settings else None,
        "max_attempts": settings.max_attempts if settings else DEFAULT_MAX_ATTEMPTS,
        # Read-only context for the settings page.
        "ai_available": actions.ai_voice_available(),
        "platform_transfer_configured": platform_transfer_number(garage) is not None,
        "supported_actions": [
            {
                "key": a.key,
                "label": a.label,
                "accepts_target": a.target != "none",
                "can_be_fallback": a.terminal,
                "available": a.available(),
            }
            for a in actions.ACTIONS.values()
        ],
    }


def update_settings(garage, data: dict) -> GarageVoiceIvrSettings:
    """Validate and save the whole menu. Raises IvrValidationError; nothing
    is written unless every part is valid."""
    options = actions.validate_options(data.get("options") or [])

    greeting = (data.get("greeting") or "").strip() or None
    if greeting is not None and len(greeting) > actions.MAX_GREETING_LENGTH:
        raise IvrValidationError(
            f"Keep the greeting under {actions.MAX_GREETING_LENGTH} characters.", field="greeting"
        )

    fallback_action = str(data.get("fallback_action") or actions.HUMAN_TRANSFER).upper()
    if fallback_action not in actions.FALLBACK_ACTIONS:
        raise IvrValidationError(
            "The fallback must transfer the caller or connect them to the assistant.",
            field="fallback_action",
        )

    raw_fallback_target = (data.get("fallback_target") or "").strip()
    fallback_target = (
        actions.normalise_transfer_number(raw_fallback_target, field="fallback_target")
        if raw_fallback_target
        else None
    )

    max_attempts = data.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if not isinstance(max_attempts, int) or not (
        actions.MIN_ATTEMPTS <= max_attempts <= actions.MAX_ATTEMPTS
    ):
        raise IvrValidationError(
            f"Attempts must be between {actions.MIN_ATTEMPTS} and {actions.MAX_ATTEMPTS}.",
            field="max_attempts",
        )

    enabled = bool(data.get("enabled"))
    if enabled:
        if not options:
            raise IvrValidationError(
                "Add at least one option before turning the menu on.", field="options"
            )
        has_default_transfer = bool(
            fallback_target
            or platform_transfer_number(garage)
            or any(o["action"] == actions.HUMAN_TRANSFER and o["target"] for o in options)
        )
        for index, option in enumerate(options):
            if option["action"] == actions.HUMAN_TRANSFER and not (
                option["target"] or has_default_transfer
            ):
                raise IvrValidationError(
                    "Add the number this option should transfer to.",
                    field=f"options.{index}.target",
                )
        if fallback_action == actions.HUMAN_TRANSFER and not has_default_transfer:
            raise IvrValidationError(
                "Add a fallback number so callers who don't choose an option reach a person.",
                field="fallback_target",
            )

    settings = get_settings(garage.id)
    if settings is None:
        settings = GarageVoiceIvrSettings(garage_id=garage.id)
        db.session.add(settings)
    settings.enabled = enabled
    settings.greeting = greeting
    settings.options = options
    settings.fallback_action = fallback_action
    settings.fallback_target = fallback_target
    settings.max_attempts = max_attempts
    db.session.commit()
    return settings
