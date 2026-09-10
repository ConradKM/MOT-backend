"""Voice provisioning: buy a number into a business's subaccount and point it
at CoMaz's **existing** webhooks.

Nothing here defines a new webhook. The URLs written onto a Twilio number are
exactly the routes ``app/communications/voice_webhooks.py`` has always served:

* ``POST /api/webhooks/twilio/voice/incoming`` - the call itself,
* ``POST /api/webhooks/twilio/voice/status``  - call progress,

both built from ``PUBLIC_API_BASE_URL``, the same value
``app/communications/security.py`` validates Twilio's signature against, so
the URL Twilio calls and the URL the signature is checked for agree by
construction.

The number is bought **into the business's own subaccount** rather than into
the platform account and transferred: a number that has only ever belonged to
one subaccount cannot be accidentally shared, and the classic REST API lets
the parent account do this directly (see
``subaccounts.py::get_client_for_subaccount_resources``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import current_app
from twilio.base.exceptions import TwilioRestException

from app.communications.config import is_twilio_configured
from app.extensions import db
from app.models.garage import Garage

from .subaccounts import SubaccountError, get_client_for_subaccount_resources

logger = logging.getLogger(__name__)

VOICE_INCOMING_PATH = "/api/webhooks/twilio/voice/incoming"
VOICE_STATUS_PATH = "/api/webhooks/twilio/voice/status"


class VoiceProvisioningError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _twilio_error(exc: TwilioRestException, fallback: str) -> VoiceProvisioningError:
    return VoiceProvisioningError(
        exc.msg or fallback, code=str(exc.code) if exc.code is not None else None
    )


def public_base_url() -> str:
    return (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")


def webhook_urls() -> dict[str, str]:
    """The two URLs a provisioned number is pointed at."""
    base = public_base_url()
    return {
        "voice_url": f"{base}{VOICE_INCOMING_PATH}",
        "status_callback": f"{base}{VOICE_STATUS_PATH}",
    }


def webhooks_reachable() -> bool:
    """Whether ``PUBLIC_API_BASE_URL`` is something Twilio could actually
    call. A localhost origin is a configuration mistake worth catching before
    a number is bought, not after a customer's first call goes unanswered."""
    base = public_base_url()
    return base.startswith("https://") and "localhost" not in base and "127.0.0.1" not in base


def search_available_numbers(
    garage: Garage,
    *,
    country: str | None = None,
    area_code: str | None = None,
    contains: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Numbers this business could buy, straight from Twilio.

    Read-only - nothing is reserved. Voice capability is required because the
    number's whole purpose here is inbound calling; SMS capability is
    reported but not required, since it only matters for the WhatsApp OTP
    shortcut.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    iso_country = (country or current_app.config.get("TWILIO_VOICE_COUNTRY") or "GB").upper()

    kwargs: dict[str, object] = {"voice_enabled": True, "limit": max(1, min(limit, 30))}
    if area_code and area_code.isdigit():
        kwargs["area_code"] = int(area_code)
    if contains:
        kwargs["contains"] = contains

    try:
        found = client.available_phone_numbers(iso_country).local.list(**kwargs)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list available numbers.") from exc

    return [
        {
            "phone_number": number.phone_number,
            "friendly_name": number.friendly_name,
            "locality": getattr(number, "locality", None),
            "region": getattr(number, "region", None),
            "iso_country": iso_country,
            "capabilities": _capability_list(getattr(number, "capabilities", None)),
        }
        for number in found
    ]


def _capability_list(capabilities: object) -> list[str]:
    """Twilio reports capabilities as a dict of flags; we keep the enabled
    names in a stable order so the console can show "voice, SMS, MMS"."""
    if not isinstance(capabilities, dict):
        return []
    order = ("voice", "SMS", "sms", "MMS", "mms", "fax")
    seen: list[str] = []
    for key in order:
        if capabilities.get(key) and key.lower() not in {s.lower() for s in seen}:
            seen.append(key)
    return seen


def buy_number(garage: Garage, phone_number: str) -> dict:
    """Purchase ``phone_number`` into this business's subaccount, already
    pointed at CoMaz's webhooks.

    The webhook URLs are set in the same call as the purchase rather than in
    a follow-up request, so there is no window where the number is live and
    answering with Twilio's default demo message.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    urls = webhook_urls()

    try:
        number = client.incoming_phone_numbers.create(
            phone_number=phone_number,
            friendly_name=f"CoMaz — {garage.name}"[:64],
            voice_url=urls["voice_url"],
            voice_method="POST",
            status_callback=urls["status_callback"],
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to buy that number.") from exc

    logger.info(
        "[provisioning] bought voice number %s (%s) for garage %s",
        number.phone_number,
        number.sid,
        garage.id,
    )
    return {
        "phone_number": number.phone_number,
        "sid": number.sid,
        "capabilities": _capability_list(getattr(number, "capabilities", None)),
    }


def assign_existing_number(garage: Garage, phone_number: str) -> dict:
    """Adopt a number the subaccount already owns, instead of buying one.

    Looked up inside the business's own subaccount, so a number belonging to
    a different tenant simply isn't found - the tenant boundary is the search
    scope, not a check that could be forgotten.
    """
    client = get_client_for_subaccount_resources(garage)
    try:
        matches = client.incoming_phone_numbers.list(phone_number=phone_number, limit=1)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not look that number up.") from exc

    if not matches:
        raise VoiceProvisioningError(
            f"{phone_number} is not owned by this business's Twilio subaccount."
        )

    number = matches[0]
    return {
        "phone_number": number.phone_number,
        "sid": number.sid,
        "capabilities": _capability_list(getattr(number, "capabilities", None)),
    }


def configure_number(
    garage: Garage, number_sid: str, *, fallback_number: str | None = None
) -> dict:
    """(Re)point an owned number at CoMaz's incoming and status webhooks.

    Safe to run repeatedly - it is the "Configure voice" repair action as
    much as a setup step, and is what an operator runs after this deployment
    moves origin.

    ``fallback_number`` is *not* written as a Twilio voice fallback URL: a
    fallback URL would have to serve TwiML from this same deployment, which is
    precisely what is unavailable when the fallback fires. Instead the number
    is left with no fallback URL, and the escalation/fallback numbers are
    stored on the business and dialled by the incoming webhook itself.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    urls = webhook_urls()

    try:
        number = client.incoming_phone_numbers(number_sid).update(
            voice_url=urls["voice_url"],
            voice_method="POST",
            status_callback=urls["status_callback"],
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to update the number's webhooks.") from exc

    return {
        "phone_number": number.phone_number,
        "sid": number.sid,
        "voice_url": urls["voice_url"],
        "status_callback": urls["status_callback"],
        "configured_at": datetime.now(UTC),
    }


def release_state_on_error(garage: Garage) -> None:  # pragma: no cover - trivial
    """Nothing to unwind: a failed purchase leaves no Twilio resource, and a
    failed configure leaves the number exactly as it was. Kept as an explicit
    statement of that, so nobody adds a speculative rollback later."""
    db.session.rollback()


def place_test_call(garage: Garage, to_number: str) -> dict:
    """Ring ``to_number`` from this business's own voice number.

    The point is to prove the whole loop: Twilio accepts the call from the
    subaccount, CoMaz's status callback comes back signed and is written to
    ``communication_logs`` by the existing handler. The TwiML is a short
    spoken confirmation served inline, so this needs no extra route.
    """
    settings = garage.communication_settings
    from_number = settings.voice_phone_number if settings else None
    if not from_number:
        raise VoiceProvisioningError("This business has no voice number to call from yet.")

    client = get_client_for_subaccount_resources(garage)
    urls = webhook_urls()
    twiml = (
        "<Response><Say>This is a CoMaz test call for "
        f"{_escape(garage.name)}. Voice setup is working.</Say></Response>"
    )

    try:
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            twiml=twiml,
            status_callback=urls["status_callback"],
            status_callback_method="POST",
            status_callback_event=["completed"],
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to place the test call.") from exc

    return {"call_sid": call.sid, "status": call.status, "to": to_number, "from": from_number}


def _escape(value: str) -> str:
    """Minimal XML escaping for the inline test TwiML - a business called
    "Smith & Sons" must not produce malformed XML."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def fetch_number(garage: Garage, number_sid: str) -> dict | None:
    """The current state of an owned number at Twilio, for Check status."""
    try:
        client = get_client_for_subaccount_resources(garage)
        number = client.incoming_phone_numbers(number_sid).fetch()
    except SubaccountError:
        return None
    except TwilioRestException:
        return None
    return {
        "phone_number": number.phone_number,
        "sid": number.sid,
        "voice_url": number.voice_url,
        "status_callback": number.status_callback,
        "capabilities": _capability_list(getattr(number, "capabilities", None)),
    }
