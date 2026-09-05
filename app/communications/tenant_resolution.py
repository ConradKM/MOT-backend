"""Resolve which garage a Twilio webhook belongs to.

Twilio tells us only *which of our numbers/senders* it was calling - never a
garage id directly - so every inbound webhook has to look up the tenant from
the destination address before it can do anything else. This is the one and
only place that lookup happens; both webhook blueprints call through here
rather than querying ``GarageCommunicationSettings`` themselves, so the
resolution rule (and the tenant-isolation guarantee that comes with it) only
has to be correct in one place.

An unresolved number is always treated as "no such tenant", never an error -
a wrong/unconfigured/stale number is expected background noise (a
disconnected trial number, a typo during manual Twilio console setup), not
grounds for a 500.
"""

from __future__ import annotations

from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage


def resolve_garage_by_voice_number(to_number: str) -> Garage | None:
    """The garage whose ``voice_phone_number`` is ``to_number`` (the Twilio
    ``To`` field of an incoming call), or ``None`` if it matches no garage."""
    if not to_number:
        return None
    settings = GarageCommunicationSettings.query.filter_by(
        voice_phone_number=to_number
    ).first()
    return settings.garage if settings else None


def resolve_garage_by_whatsapp_sender(to_number: str) -> Garage | None:
    """The garage whose ``whatsapp_sender`` is ``to_number`` (the Twilio
    ``To`` field of an incoming WhatsApp message, e.g. ``"whatsapp:+1415…"``),
    or ``None`` if it matches no garage."""
    if not to_number:
        return None
    settings = GarageCommunicationSettings.query.filter_by(
        whatsapp_sender=to_number
    ).first()
    return settings.garage if settings else None


def resolve_twilio_resources(garage: Garage) -> dict[str, str | None]:
    """The chain business -> subaccount SID -> phone number -> WhatsApp
    sender, for anything that needs to display or reason about a garage's
    Twilio setup (without reaching into the settings row's column names
    directly). All values are ``None`` for a garage with no row yet."""
    settings = garage.communication_settings
    if settings is None:
        return {
            "twilio_subaccount_sid": None,
            "voice_phone_number": None,
            "whatsapp_sender": None,
            "messaging_service_sid": None,
        }
    return {
        "twilio_subaccount_sid": settings.twilio_subaccount_sid,
        "voice_phone_number": settings.voice_phone_number,
        "whatsapp_sender": settings.whatsapp_sender,
        "messaging_service_sid": settings.messaging_service_sid,
    }
