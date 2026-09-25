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

import logging

from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    DIRECTION_INBOUND,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage
from app.phone import e164_from_address

logger = logging.getLogger(__name__)

# Mirrors app/communications/telephony.py::MODE_SIP_BYOC - not imported, so
# this module stays importable from anywhere without a cycle.
_MODE_SIP_BYOC = "SIP_BYOC"


def resolve_garage_by_voice_number(to_number: str) -> Garage | None:
    """The garage whose ``voice_phone_number`` is ``to_number`` (the Twilio
    ``To`` field of an incoming call), or ``None`` if it matches no garage."""
    if not to_number:
        return None
    settings = GarageCommunicationSettings.query.filter_by(voice_phone_number=to_number).first()
    return settings.garage if settings else None


def resolve_garage_for_inbound_call(form) -> Garage | None:
    """The business an inbound Twilio Voice webhook belongs to. Fails closed.

    * A call that arrived over SIP (Twilio sends ``SipDomainSid``) is a
      SIP/BYOC call: the carrier keeps the business's number and delivers it
      as the called number. It belongs to a business only when that business
      is SIP_BYOC, the call came in on *its* SIP domain, and the called number
      is *its* public number. Twilio discards custom SIP headers from BYOC
      carriers, and nothing else in the request is used - so neither a
      caller nor a carrier can pick another tenant.
    * Anything else is a PSTN call to a number CoMaz owns (a CoMaz number,
      or the ingress a business's public number forwards to), matched
      exactly as it always has been.

    Every request reaching here has already passed Twilio signature
    validation, so ``To``/``SipDomainSid`` describe a real call on CoMaz's
    own Twilio account."""
    sip_domain_sid = (form.get("SipDomainSid") or "").strip()
    if sip_domain_sid:
        called = e164_from_address(form.get("To"))
        if not called:
            return None
        settings = GarageCommunicationSettings.query.filter_by(
            telephony_mode=_MODE_SIP_BYOC,
            byoc_sip_domain_sid=sip_domain_sid,
            public_business_number=called,
        ).first()
        return settings.garage if settings else None
    return resolve_garage_by_voice_number(form.get("To", ""))


def garage_pinned_to_call(call_sid: str | None) -> Garage | None:
    """The business ``/incoming`` resolved this Twilio call to, from the
    call-level row it logged - so every later step of the same call uses the
    tenant chosen once, under the full rules above."""
    if not call_sid:
        return None
    row = CommunicationLog.query.filter_by(
        external_id=call_sid,
        external_provider="twilio",
        channel=CHANNEL_VOICE,
        direction=DIRECTION_INBOUND,
    ).first()
    return row.garage if row is not None else None


def resolve_garage_for_call_step(form) -> Garage | None:
    """The business a follow-up webhook of an in-progress call (a menu key
    press, a finished transfer) belongs to.

    Prefers the tenant pinned to the CallSid at ``/incoming``; a Twilio
    action callback for a BYOC call need not repeat ``SipDomainSid``, and
    the pin makes that irrelevant. If the request would also resolve on its
    own and names a *different* business, nothing is trusted."""
    pinned = garage_pinned_to_call(form.get("CallSid"))
    resolved = resolve_garage_for_inbound_call(form)
    if pinned is not None and resolved is not None and pinned.id != resolved.id:
        logger.warning(
            "VOICE_TENANT_MISMATCH callSid=%s pinned=%s resolved=%s",
            form.get("CallSid"),
            pinned.id,
            resolved.id,
        )
        return None
    return pinned or resolved


def resolve_garage_by_whatsapp_sender(to_number: str) -> Garage | None:
    """The garage whose ``whatsapp_sender`` is ``to_number`` (the Twilio
    ``To`` field of an incoming WhatsApp message, e.g. ``"whatsapp:+1415…"``),
    or ``None`` if it matches no garage."""
    if not to_number:
        return None
    settings = GarageCommunicationSettings.query.filter_by(whatsapp_sender=to_number).first()
    return settings.garage if settings else None


def resolve_garage_by_sms_sender(to_number: str) -> Garage | None:
    """The garage an inbound SMS's Twilio ``To`` field belongs to.

    SMS has no sender field of its own (see TwilioSMSProvider) - it reuses
    whichever of ``messaging_service_sid``/``voice_phone_number`` a garage
    configured for outbound SMS, so an inbound SMS is matched against either
    column, not a dedicated one."""
    if not to_number:
        return None
    settings = GarageCommunicationSettings.query.filter_by(voice_phone_number=to_number).first()
    if settings is None:
        settings = GarageCommunicationSettings.query.filter_by(
            messaging_service_sid=to_number
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
