"""Twilio configuration gate.

Every send path and every webhook checks ``is_twilio_configured()`` first and
degrades safely (skip-and-log, or a 503) rather than ever calling the Twilio
SDK with empty credentials. This is what makes "no Twilio account yet" a
normal, permanent, crash-free way to run CoMaz OS - not just a dev-mode
special case.
"""

from flask import current_app


def is_twilio_configured() -> bool:
    """Whether the platform (master) Twilio account is usable - both
    TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are set."""
    cfg = current_app.config
    return bool(cfg.get("TWILIO_ACCOUNT_SID")) and bool(cfg.get("TWILIO_AUTH_TOKEN"))


def garage_communications_enabled(garage) -> bool:
    """Whether ``garage`` has opted into communications at all. Does not by
    itself mean a given channel can be used - e.g. sending WhatsApp still
    needs ``whatsapp_sender`` set (see app/communications/service.py)."""
    settings = garage.communication_settings
    return bool(settings and settings.communications_enabled)
