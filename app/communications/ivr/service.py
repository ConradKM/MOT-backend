"""Reading, validating and saving a business's phone menu."""

from __future__ import annotations

from app.extensions import db
from app.models.communications.voice_ivr_settings import GarageVoiceIvrSettings

from .. import telephony
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
    """The platform-configured human destination a transfer option without
    its own number uses: the primary destination (a phone number or SIP
    address), else the legacy escalation/fallback number."""
    comm = getattr(garage, "communication_settings", None)
    if comm is None:
        return None
    if comm.human_primary_type and comm.human_primary_destination:
        return str(comm.human_primary_destination)
    return comm.voice_escalation_number or comm.voice_fallback_number or None


def transfer_target(
    garage, settings: GarageVoiceIvrSettings | None, option: dict | None = None
) -> str | None:
    """The first destination a transfer rings - the head of
    telephony.human_destinations, which also owns the full ordered chain,
    loop prevention and the attempt limit."""
    chain = telephony.human_destinations(garage, settings, option)
    return chain[0].value if chain else None


def _reject_comaz_numbers(options: list[dict], fallback_target: str | None) -> None:
    """An owner's transfer number may never be one that rings CoMaz itself -
    their own forwarded public number, a CoMaz number, or another business's
    - or the caller would loop straight back into a phone menu."""
    routed = telephony.comaz_routed_numbers()
    message = (
        "That number rings this phone system itself, so callers would loop back "
        "to the menu. Use a phone the team actually answers."
    )
    for index, option in enumerate(options):
        if option.get("target") and option["target"] in routed:
            raise IvrValidationError(message, field=f"options.{index}.target")
    if fallback_target and fallback_target in routed:
        raise IvrValidationError(message, field="fallback_target")


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
    _reject_comaz_numbers(options, fallback_target)

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
