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

from app.communications.client import get_twilio_account_management_client
from app.communications.config import is_twilio_configured
from app.extensions import db
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage

from .subaccounts import SubaccountError, get_client_for_subaccount_resources

logger = logging.getLogger(__name__)

VOICE_INCOMING_PATH = "/api/webhooks/twilio/voice/incoming"
VOICE_STATUS_PATH = "/api/webhooks/twilio/voice/status"


class VoiceProvisioningError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class MissingBusinessAddressError(VoiceProvisioningError):
    """The selected number requires a Twilio-registered business address and
    this tenant's stored address is incomplete. Raised *before* any Twilio
    purchase call, so the operator sees an actionable prompt instead of a raw
    provider error."""


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
            # "none" | "any" | "local" | "foreign" - Twilio's own regulatory
            # classification of what AddressSid, if any, buy_number() will
            # need to supply for this specific number.
            "address_requirements": getattr(number, "address_requirements", "none") or "none",
        }
        for number in found
    ]


def claimed_by_other_garage(phone_number: str, garage: Garage) -> Garage | None:
    """The *other* CoMaz business already recording this number, if any.

    Belt-and-braces: ``voice_phone_number`` already carries a database
    unique constraint, so this can never be bypassed - but a clear refusal
    here is better than surfacing that constraint's raw IntegrityError to an
    admin picking a number from a list.
    """
    row = (
        db.session.query(GarageCommunicationSettings)
        .filter(
            GarageCommunicationSettings.voice_phone_number == phone_number,
            GarageCommunicationSettings.garage_id != garage.id,
        )
        .first()
    )
    return row.garage if row else None


def whatsapp_configured_elsewhere(phone_number: str) -> bool:
    """Whether *any* CoMaz business has this number registered as a
    WhatsApp sender - including this same business, since a number moving
    accounts at Twilio never moves its WhatsApp sender configuration with
    it (Twilio's own documented behaviour). A caller must treat this as a
    reason to stop and require explicit confirmation, never to silently
    transfer or reconfigure."""
    return (
        db.session.query(GarageCommunicationSettings.id)
        .filter(GarageCommunicationSettings.whatsapp_sender == phone_number)
        .first()
        is not None
    )


def _number_summary(number, *, whatsapp_flag: bool = True) -> dict:
    return {
        "phone_number": number.phone_number,
        "sid": number.sid,
        "friendly_name": getattr(number, "friendly_name", None),
        "capabilities": _capability_list(getattr(number, "capabilities", None)),
        "whatsapp_configured": (
            whatsapp_configured_elsewhere(number.phone_number) if whatsapp_flag else False
        ),
    }


def list_owned_numbers(garage: Garage) -> list[dict]:
    """Numbers already owned by this business's own Twilio subaccount -
    Case 1 of "Use existing number": nothing to buy or transfer, only to
    adopt and configure."""
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    try:
        numbers = client.incoming_phone_numbers.list(limit=50)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list this business's numbers.") from exc

    return [_number_summary(n) for n in numbers]


def list_parent_numbers() -> list[dict]:
    """Numbers owned by CoMaz's own parent Twilio account - Case 2 of "Use
    existing number": available to move into a business's subaccount, but
    never automatically.

    Never scoped to a garage: the parent account is shared platform
    infrastructure, not any one business's resource.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_twilio_account_management_client()
    if client is None:  # pragma: no cover - guarded by is_twilio_configured above
        raise VoiceProvisioningError("Twilio client unavailable.")

    try:
        numbers = client.incoming_phone_numbers.list(limit=50)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list the parent account's numbers.") from exc

    return [_number_summary(n) for n in numbers]


def transfer_from_parent(garage: Garage, phone_number_sid: str) -> dict:
    """Move a number Twilio shows as owned by CoMaz's parent account into
    this business's own subaccount, then point it at CoMaz's webhooks.

    Uses Twilio's own documented mechanism for moving a number between
    accounts: updating the number resource's ``account_sid``, authenticated
    with credentials that can act on its *current* owning account - here,
    the parent account client, exactly like ``list_parent_numbers`` uses.

    Fails closed rather than trusting the caller's claim that this number is
    parent-owned: the number is re-fetched from Twilio first, and the
    transfer is refused unless Twilio itself currently reports it under the
    parent account. A number already moved elsewhere (by a concurrent
    request, or by hand in the console) is never silently moved again.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    settings = garage.communication_settings
    subaccount_sid = settings.twilio_subaccount_sid if settings else None
    if not subaccount_sid:
        raise VoiceProvisioningError(
            "Create this business's Twilio subaccount before transferring a number to it."
        )

    parent_client = get_twilio_account_management_client()
    if parent_client is None:  # pragma: no cover - guarded above
        raise VoiceProvisioningError("Twilio client unavailable.")

    parent_account_sid = current_app.config["TWILIO_ACCOUNT_SID"]

    try:
        current = parent_client.incoming_phone_numbers(phone_number_sid).fetch()
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio could not find that number on the parent account."
        ) from exc

    if getattr(current, "account_sid", None) != parent_account_sid:
        raise VoiceProvisioningError(
            f"{current.phone_number} is no longer owned by CoMaz's parent account - it may "
            "already have been moved. Refresh and try again."
        )

    other = claimed_by_other_garage(current.phone_number, garage)
    if other is not None:
        raise VoiceProvisioningError(f"{current.phone_number} is already assigned to {other.name}.")

    try:
        moved = parent_client.incoming_phone_numbers(phone_number_sid).update(
            account_sid=subaccount_sid
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to move that number to this business.") from exc

    logger.info(
        "[provisioning] transferred voice number %s (%s) from parent account to garage %s "
        "subaccount %s",
        moved.phone_number,
        moved.sid,
        garage.id,
        subaccount_sid,
    )

    # Now owned by the subaccount - point it at CoMaz's webhooks the same
    # way a fresh purchase or an in-subaccount adoption does.
    return configure_number(garage, moved.sid)


def return_to_parent(garage: Garage, number_sid: str) -> dict:
    """Move ``number_sid`` out of this business's subaccount and back into
    CoMaz's shared parent account - the inverse of ``transfer_from_parent``,
    and the only supported way a number leaves a business: never released
    or deleted, always moved to a place the existing discovery flow
    (``list_parent_numbers``) already finds it again.

    Idempotent against a prior attempt that moved the number at Twilio but
    never got the chance to record it: if this subaccount no longer shows
    the number as its own, that's treated as "already returned" rather than
    an error - the caller (``action_return_voice_number_to_parent``) still
    needs to clear its own stale local state either way, which is exactly
    what a retry of this action does.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    try:
        current = client.incoming_phone_numbers(number_sid).fetch()
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio could not find that number in this business's subaccount."
        ) from exc

    parent_account_sid = current_app.config["TWILIO_ACCOUNT_SID"]
    if current.account_sid == parent_account_sid:
        # Already moved - a prior attempt's Twilio call succeeded but its
        # result was never recorded. Nothing left to do at the provider.
        logger.info(
            "[provisioning] voice number %s (%s) was already back on the parent account for "
            "garage %s",
            current.phone_number,
            current.sid,
            garage.id,
        )
        return {"phone_number": current.phone_number, "sid": current.sid}

    try:
        moved = client.incoming_phone_numbers(number_sid).update(account_sid=parent_account_sid)
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio refused to return that number to CoMaz's parent account."
        ) from exc

    logger.info(
        "[provisioning] returned voice number %s (%s) from garage %s to the parent account",
        moved.phone_number,
        moved.sid,
        garage.id,
    )
    return {"phone_number": moved.phone_number, "sid": moved.sid}


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


def _garage_address_fields(garage: Garage) -> tuple[str, str, str, str] | None:
    """``(street, city, region, postal_code)`` from this tenant's stored
    business address, or ``None`` if any required part is missing.

    Deliberately does not attempt to derive city/region from the freeform
    ``address`` line - a wrong guess would be regulatory data submitted to
    Twilio, not a cosmetic mistake."""
    street = (garage.address or "").strip()
    city = (garage.address_city or "").strip()
    region = (garage.address_region or "").strip()
    postal_code = (garage.postcode or "").strip()
    if not (street and city and region and postal_code):
        return None
    return street, city, region, postal_code


def _ensure_address(garage: Garage, client, iso_country: str) -> str:
    """The Twilio AddressSid to buy a regulated number with, reusing an
    existing matching Address resource in this subaccount rather than
    creating a new one on every Buy click (Twilio does not deduplicate these
    itself - two identical Address resources are perfectly legal to it)."""
    fields = _garage_address_fields(garage)
    if fields is None:
        raise MissingBusinessAddressError(
            "This number requires a registered business address. Add this "
            "business's street, city, region and postcode under Tenant "
            "details, then try buying the number again.",
            code="address_incomplete",
        )
    street, city, region, postal_code = fields

    try:
        existing = client.addresses.list(limit=50)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not check for an existing address.") from exc

    for addr in existing:
        if (
            (addr.street or "").strip().lower() == street.lower()
            and (addr.city or "").strip().lower() == city.lower()
            and (addr.postal_code or "").strip().lower() == postal_code.lower()
            and addr.iso_country == iso_country
        ):
            return addr.sid

    try:
        created = client.addresses.create(
            customer_name=garage.name[:100],
            street=street,
            city=city,
            region=region,
            postal_code=postal_code,
            iso_country=iso_country,
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to register that business address.") from exc

    logger.info("[provisioning] created Twilio address %s for garage %s", created.sid, garage.id)
    return created.sid


def _address_requirements_for(client, iso_country: str, phone_number: str) -> str:
    """Re-checks Twilio's own regulatory classification for this exact
    number at purchase time, rather than trusting a value the operator's
    browser may have cached from an earlier search."""
    try:
        found = client.available_phone_numbers(iso_country).local.list(
            contains=phone_number, limit=1
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not check that number's requirements.") from exc
    if not found:
        # No longer listed as available - either already bought (handled by
        # the reconciliation check right after this) or gone. "none" is safe
        # here: if it's actually still unbought and does need an address,
        # Twilio's own purchase call will refuse it, not silently succeed.
        return "none"
    return getattr(found[0], "address_requirements", "none") or "none"


def buy_number(garage: Garage, phone_number: str) -> dict:
    """Purchase ``phone_number`` into this business's subaccount, already
    pointed at CoMaz's webhooks.

    The webhook URLs are set in the same call as the purchase rather than in
    a follow-up request, so there is no window where the number is live and
    answering with Twilio's default demo message.

    Reconciles before buying: if Twilio already shows this exact number
    owned by this subaccount - the purchase succeeded on a previous attempt
    but the response was lost, or CoMaz's own commit that would have
    recorded it never landed - that existing resource is adopted instead of
    buying a second one. This is what makes a retry after a crash or a
    network timeout recover the already-paid-for number rather than
    doubling the charge; ``action_buy_voice_number``'s own guard already
    covers the simpler case of a plain retry once the first purchase *did*
    get recorded.
    """
    if not is_twilio_configured():
        raise VoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_client_for_subaccount_resources(garage)
    urls = webhook_urls()
    iso_country = (current_app.config.get("TWILIO_VOICE_COUNTRY") or "GB").upper()

    try:
        existing = client.incoming_phone_numbers.list(phone_number=phone_number, limit=1)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not check for an existing purchase.") from exc

    if existing:
        # Already owned by this subaccount from an earlier attempt whose
        # result CoMaz never recorded - reuse it rather than buying again.
        # (A number owned by a *different* subaccount would never appear
        # here: this client is scoped to this business's own subaccount.)
        number = existing[0]
        try:
            number = number.update(
                voice_url=urls["voice_url"],
                voice_method="POST",
                status_callback=urls["status_callback"],
                status_callback_method="POST",
            )
        except TwilioRestException as exc:
            raise _twilio_error(exc, "Twilio refused to reconfigure that number.") from exc
        logger.info(
            "[provisioning] reconciled already-owned voice number %s (%s) for garage %s "
            "instead of buying again",
            number.phone_number,
            number.sid,
            garage.id,
        )
    else:
        create_kwargs: dict[str, object] = {
            "phone_number": phone_number,
            "friendly_name": f"CoMaz — {garage.name}"[:64],
            "voice_url": urls["voice_url"],
            "voice_method": "POST",
            "status_callback": urls["status_callback"],
            "status_callback_method": "POST",
        }

        requirements = _address_requirements_for(client, iso_country, phone_number)
        if requirements != "none":
            # Raises MissingBusinessAddressError *before* any Twilio spend
            # if the tenant's address isn't complete enough - a purchase
            # must never be attempted only to fail on a missing AddressSid.
            create_kwargs["address_sid"] = _ensure_address(garage, client, iso_country)

        try:
            number = client.incoming_phone_numbers.create(**create_kwargs)
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
