"""Tenant resolution for an inbound OpenAI Realtime SIP call.

Reuses the exact same canonical business-number mapping every other voice
path already uses (app/communications/tenant_resolution.py::
resolve_garage_by_voice_number) - a business's ``voice_phone_number`` means
the same thing whether a call reaches it via Twilio's Programmable Voice
webhook or via a Twilio Elastic SIP Trunk routed straight to OpenAI.
"""

from __future__ import annotations

from app.communications.tenant_resolution import resolve_garage_by_voice_number
from app.models.garage import Garage
from app.phone import InvalidPhoneNumberError, normalize_uk_phone


def _header_field(header, field: str) -> str | None:
    """Works whether ``header`` is the SDK's parsed ``DataSipHeader`` object
    (attribute access) or a plain ``{"name", "value"}`` dict (tests, or a
    hand-built event) - never assumes one or the other."""
    if isinstance(header, dict):
        return header.get(field)
    return getattr(header, field, None)


def sip_header(sip_headers: list, name: str) -> str | None:
    """One header's value from the ``realtime.call.incoming`` event's
    ``data.sip_headers`` - a list of name/value pairs, not a dict (see
    https://developers.openai.com/api/docs/guides/voice-sip)."""
    for header in sip_headers or []:
        if str(_header_field(header, "name") or "").lower() == name.lower():
            value = _header_field(header, "value")
            return str(value) if value is not None else None
    return None


def phone_from_sip_uri(value: str | None) -> str | None:
    """The phone-number portion of a ``sip:+18005551212@host`` / ``tel:...``
    URI. This (and every SIP header) is untrusted caller-supplied metadata
    per OpenAI's own guidance - it only ever drives a business lookup below,
    never an authorization decision by itself.

    Real SIP headers (unlike the bare-URI shape it's easy to test with) are
    routinely the RFC 3261 name-addr form - the URI wrapped in ``<...>``,
    with header parameters like ``;tag=`` or ``;reason=`` *outside* the
    brackets, e.g. ``<sip:+441234567890@host>;tag=abc``. Strip that wrapper
    first; a real Diversion/To header parsed without doing this yields
    garbage (see the ``;tag=``/``;reason=`` suffix ending up glued onto the
    "number" otherwise)."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        value = value[1:end] if end != -1 else value[1:]
    for prefix in ("sips:", "sip:", "tel:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    number = value.split("@", 1)[0].split(";", 1)[0]
    return number or None


def resolve_business_for_sip_call(sip_headers: list) -> Garage | None:
    """The garage this inbound SIP call's originally-dialled number belongs
    to, or ``None`` if it doesn't unambiguously map to a business CoMaz
    knows about (an unrecognised number, or one that fails to parse as a
    real UK number) - the caller (app/ai_voice/routes.py) must reject the
    call in that case, never guess or fall back to any default business.

    Prefers the ``Diversion`` header over ``To``: for a Twilio Elastic SIP
    Trunk's Origination call (this integration's only supported inbound
    path - see docs/OPENAI_VOICE_SETUP.md), Twilio's own SIP Trunking docs
    guarantee "the Twilio number dialled will always be conveyed in a SIP
    Diversion header" - the ``To``/Request-URI Twilio actually sends is the
    fixed Origination URI itself (``sip:$OPENAI_PROJECT_ID@sip.api.openai.com``),
    the same for every call on the trunk regardless of which CoMaz number
    was dialled, so resolving off ``To`` alone can never identify the
    tenant for this call path (confirmed live: callSid
    rtc_u0_EPvuUp3oMjxHm4KyxnDpSQVfbHbOEtbS on 2026-09-19 rejected with
    AI_VOICE_TENANT_UNRESOLVED because ``To`` was that fixed project URI,
    not the dialled +443330382135). ``To`` is kept as a fallback for any
    other SIP source that might reach this webhook without a Diversion
    header, rather than dropped outright."""
    to_number = phone_from_sip_uri(sip_header(sip_headers, "Diversion")) or phone_from_sip_uri(
        sip_header(sip_headers, "To")
    )
    if not to_number:
        return None
    try:
        normalised = normalize_uk_phone(to_number)
    except InvalidPhoneNumberError:
        return None
    return resolve_garage_by_voice_number(normalised)


def caller_number_for_sip_call(sip_headers: list) -> str:
    """Best-effort caller number for logging/tools (e.g. defaulting
    create_booking's contact phone) - never used for tenant resolution, and
    never trusted beyond "this is what the caller's device/network claimed".
    Empty string (not None) when absent, so callers can use it directly as
    a dict/string value without a None-check at every call site."""
    from_number = phone_from_sip_uri(sip_header(sip_headers, "From"))
    if not from_number:
        return ""
    try:
        return normalize_uk_phone(from_number)
    except InvalidPhoneNumberError:
        return from_number
