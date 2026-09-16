"""Provider-neutral communications orchestration and persistence.

Provider adapters own transport calls, credentials, and SDK error translation.
Booking and automation callers keep the existing CommunicationLog contract. Every public
function here always returns a :class:`CommunicationLog` (never raises for a
Twilio-side failure) so callers never need special-case error handling around
"did this actually send" - they read the row's ``status`` if they care.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_SMS,
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    STATUS_SKIPPED_NOT_CONFIGURED,
    CommunicationLog,
)
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile

from .providers import get_messaging_provider, get_sms_provider, get_voice_provider, provider_name
from .providers.base import InboundEvent, ProviderFailure, StatusEvent

if TYPE_CHECKING:
    from app.models.customer import Customer

logger = logging.getLogger(__name__)


def _create_log(**fields) -> CommunicationLog:
    log = CommunicationLog(**fields)
    db.session.add(log)
    db.session.commit()
    return log


def _related_ids(customer=None, appointment=None, booking_request=None) -> dict:
    return {
        "customer_id": customer.id if customer is not None else None,
        "appointment_id": appointment.id if appointment is not None else None,
        "booking_request_id": booking_request.id if booking_request is not None else None,
    }


def _failure_fields(exc: Exception) -> tuple[str | None, str]:
    return (exc.code if isinstance(exc, ProviderFailure) else None), str(exc)


def _skip(
    *,
    garage,
    channel,
    direction,
    to_address=None,
    from_address=None,
    body=None,
    trigger_event=None,
    customer=None,
    appointment=None,
    booking_request=None,
    reason: str,
) -> CommunicationLog:
    """Record a would-be send that never reached Twilio - status
    SKIPPED_NOT_CONFIGURED, never silently treated as delivered."""
    logger.info(
        "[communications] SKIPPED_NOT_CONFIGURED channel=%s garage=%s reason=%s",
        channel,
        garage.id,
        reason,
    )
    return _create_log(
        garage_id=garage.id,
        channel=channel,
        direction=direction,
        external_provider=provider_name(garage, channel.lower()),
        external_id=None,
        from_address=from_address,
        to_address=to_address,
        status=STATUS_SKIPPED_NOT_CONFIGURED,
        trigger_event=trigger_event,
        body=body,
        error_message=reason,
        **_related_ids(customer, appointment, booking_request),
    )


def find_customer_by_phone(garage, phone: str | None) -> Customer | None:
    """Best-effort inbound-message/call -> customer match, by exact phone
    string against this garage's customers.

    ``Customer.phone`` is free text today (see app/phone.py's module
    docstring), so this only catches a customer whose stored number already
    matches the E.164 form Twilio sends - not every possible formatting of
    the same number. Good enough for "associate where safely possible"; not a
    substitute for normalising Customer.phone itself.
    """
    if not phone:
        return None

    from app.models.customer import Customer

    result: Customer | None = Customer.query.filter_by(garage_id=garage.id, phone=phone).first()
    return result


def send_whatsapp_message(
    *,
    garage,
    to: str,
    body: str,
    customer=None,
    appointment=None,
    booking_request=None,
    trigger_event: str | None = None,
) -> CommunicationLog:
    """Send a WhatsApp message from ``garage``'s configured sender.

    Always returns a :class:`CommunicationLog` - a skipped send (Twilio, or
    this garage, not configured) is recorded exactly like a real attempt,
    just with status ``SKIPPED_NOT_CONFIGURED`` and no provider SID.
    """
    settings = garage.communication_settings
    provider = get_messaging_provider(garage)

    try:
        to_e164 = normalize_uk_mobile(to)
    except InvalidPhoneNumberError as exc:
        return _skip(
            garage=garage,
            channel=CHANNEL_WHATSAPP,
            direction=DIRECTION_OUTBOUND,
            to_address=to,
            body=body,
            trigger_event=trigger_event,
            customer=customer,
            appointment=appointment,
            booking_request=booking_request,
            reason=f"Invalid destination number: {exc}",
        )

    # Normalised (and, from here on, "whatsapp:"-prefixed) even for a
    # SKIPPED_NOT_CONFIGURED row - the communication log should always show a
    # clean destination number, regardless of which check below stopped it.
    to_address = f"whatsapp:{to_e164}"
    skip_kwargs = {
        "garage": garage,
        "channel": CHANNEL_WHATSAPP,
        "direction": DIRECTION_OUTBOUND,
        "to_address": to_address,
        "body": body,
        "trigger_event": trigger_event,
        "customer": customer,
        "appointment": appointment,
        "booking_request": booking_request,
    }

    reason = provider.configuration_error(garage)
    if reason:
        return _skip(**skip_kwargs, reason=reason)

    try:
        provider.capabilities.require("whatsapp")
        message = provider.send_message(garage, to=to_address, body=body)
    except Exception as exc:  # noqa: BLE001 - a send must never raise; recorded as FAILED below
        error_code, error_message = _failure_fields(exc)
        return _create_log(
            garage_id=garage.id,
            channel=CHANNEL_WHATSAPP,
            direction=DIRECTION_OUTBOUND,
            external_provider=provider.name,
            external_id=None,
            from_address=settings.whatsapp_sender,
            to_address=to_address,
            status="FAILED",
            trigger_event=trigger_event,
            body=body,
            error_code=error_code,
            error_message=error_message,
            **_related_ids(customer, appointment, booking_request),
        )

    return _create_log(
        garage_id=garage.id,
        channel=CHANNEL_WHATSAPP,
        direction=DIRECTION_OUTBOUND,
        external_provider=provider.name,
        external_id=message.interaction_id,
        from_address=settings.whatsapp_sender,
        to_address=to_address,
        status=message.status,
        trigger_event=trigger_event,
        body=body,
        **_related_ids(customer, appointment, booking_request),
    )


def send_sms_message(
    *,
    garage,
    to: str,
    body: str,
    customer=None,
    appointment=None,
    booking_request=None,
    trigger_event: str | None = None,
) -> CommunicationLog:
    """Send a plain SMS from ``garage``'s configured Twilio number. Same
    skip/error/success recording contract as :func:`send_whatsapp_message` -
    always returns a :class:`CommunicationLog`, never raises for a
    provider-side failure. Callers (see app/communications/sms_automation.py)
    are additionally expected to check ``SMS_NOTIFICATIONS_ENABLED`` before
    calling this at all; this function itself only gates on provider/garage
    configuration, exactly like every other channel here.
    """
    settings = garage.communication_settings
    provider = get_sms_provider(garage)

    try:
        to_e164 = normalize_uk_mobile(to)
    except InvalidPhoneNumberError as exc:
        return _skip(
            garage=garage,
            channel=CHANNEL_SMS,
            direction=DIRECTION_OUTBOUND,
            to_address=to,
            body=body,
            trigger_event=trigger_event,
            customer=customer,
            appointment=appointment,
            booking_request=booking_request,
            reason=f"Invalid destination number: {exc}",
        )

    skip_kwargs = {
        "garage": garage,
        "channel": CHANNEL_SMS,
        "direction": DIRECTION_OUTBOUND,
        "to_address": to_e164,
        "body": body,
        "trigger_event": trigger_event,
        "customer": customer,
        "appointment": appointment,
        "booking_request": booking_request,
    }

    reason = provider.configuration_error(garage)
    if reason:
        return _skip(**skip_kwargs, reason=reason)

    from_address = settings.messaging_service_sid or settings.voice_phone_number

    try:
        provider.capabilities.require("sms")
        message = provider.send_sms(garage, to=to_e164, body=body)
    except Exception as exc:  # noqa: BLE001 - a send must never raise; recorded as FAILED below
        error_code, error_message = _failure_fields(exc)
        return _create_log(
            garage_id=garage.id,
            channel=CHANNEL_SMS,
            direction=DIRECTION_OUTBOUND,
            external_provider=provider.name,
            external_id=None,
            from_address=from_address,
            to_address=to_e164,
            status="FAILED",
            trigger_event=trigger_event,
            body=body,
            error_code=error_code,
            error_message=error_message,
            **_related_ids(customer, appointment, booking_request),
        )

    return _create_log(
        garage_id=garage.id,
        channel=CHANNEL_SMS,
        direction=DIRECTION_OUTBOUND,
        external_provider=provider.name,
        external_id=message.interaction_id,
        from_address=from_address,
        to_address=to_e164,
        status=message.status,
        trigger_event=trigger_event,
        body=body,
        **_related_ids(customer, appointment, booking_request),
    )


def initiate_voice_call(
    *,
    garage,
    to: str,
    twiml_url: str | None = None,
    instructions_url: str | None = None,
    customer=None,
    appointment=None,
    booking_request=None,
    trigger_event: str | None = None,
) -> CommunicationLog:
    """Place an outbound voice call from ``garage``'s configured number,
    directing Twilio to fetch call instructions from ``twiml_url``. Same
    skip/error/success recording contract as :func:`send_whatsapp_message`.
    """
    settings = garage.communication_settings
    provider = get_voice_provider(garage)

    try:
        to_e164 = normalize_uk_mobile(to)
    except InvalidPhoneNumberError as exc:
        return _skip(
            garage=garage,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_OUTBOUND,
            to_address=to,
            trigger_event=trigger_event,
            customer=customer,
            appointment=appointment,
            booking_request=booking_request,
            reason=f"Invalid destination number: {exc}",
        )

    skip_kwargs = {
        "garage": garage,
        "channel": CHANNEL_VOICE,
        "direction": DIRECTION_OUTBOUND,
        "to_address": to_e164,
        "trigger_event": trigger_event,
        "customer": customer,
        "appointment": appointment,
        "booking_request": booking_request,
    }

    reason = provider.configuration_error(garage)
    if reason:
        return _skip(**skip_kwargs, reason=reason)

    try:
        provider.capabilities.require("outbound_voice")
        call = provider.initiate_call(
            garage, to=to_e164, instructions_url=instructions_url or twiml_url or ""
        )
    except Exception as exc:  # noqa: BLE001 - a send must never raise; recorded as FAILED below
        error_code, error_message = _failure_fields(exc)
        return _create_log(
            garage_id=garage.id,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_OUTBOUND,
            external_provider=provider.name,
            external_id=None,
            from_address=settings.voice_phone_number,
            to_address=to_e164,
            status="FAILED",
            trigger_event=trigger_event,
            error_code=error_code,
            error_message=error_message,
            **_related_ids(customer, appointment, booking_request),
        )

    return _create_log(
        garage_id=garage.id,
        channel=CHANNEL_VOICE,
        direction=DIRECTION_OUTBOUND,
        external_provider=provider.name,
        external_id=call.interaction_id,
        call_sid=call.interaction_id,
        from_address=settings.voice_phone_number,
        to_address=to_e164,
        status=call.status,
        trigger_event=trigger_event,
        **_related_ids(customer, appointment, booking_request),
    )


def record_inbound_communication(
    *,
    garage,
    channel: str,
    from_address: str,
    to_address: str,
    external_id: str | None,
    status: str,
    body: str | None = None,
    provider: str = "twilio",
    customer=None,
) -> CommunicationLog:
    """Log an inbound call/message a webhook just received. Always succeeds -
    there is no "send" step to fail here, only a record to keep."""
    return _create_log(
        garage_id=garage.id,
        channel=channel,
        direction=DIRECTION_INBOUND,
        external_provider=provider,
        external_id=external_id,
        # For a voice call this row is the call itself; carrying the CallSid
        # in call_sid too lets the engine's transcript-turn rows group under
        # it (see app/communications/queries.py).
        call_sid=external_id if channel == CHANNEL_VOICE else None,
        from_address=from_address,
        to_address=to_address,
        status=status,
        body=body,
        **_related_ids(customer=customer),
    )


def update_communication_status(
    *,
    external_id: str | None,
    status: str,
    provider: str = "twilio",
    call_duration_seconds: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> CommunicationLog | None:
    """Apply a Twilio status callback to the communication it belongs to,
    found by its provider SID - idempotent by construction, since a duplicate
    callback for the same SID updates that same row again rather than ever
    inserting a second one. Returns ``None`` (and logs a warning) if no row
    matches; the webhook that calls this never treats that as an error.
    """
    if not external_id:
        return None

    query = CommunicationLog.query.filter_by(external_id=external_id)
    # Legacy Twilio callbacks also update automation-owned rows whose provider
    # tag is comaz_conversation_engine. Keep that existing lookup contract.
    if provider != "twilio":
        query = query.filter_by(external_provider=provider)
    log: CommunicationLog | None = query.first()
    if log is None:
        logger.warning("[communications] status callback for unknown external_id=%s", external_id)
        return None

    log.status = status
    if call_duration_seconds is not None:
        log.call_duration_seconds = call_duration_seconds
    if error_code:
        log.error_code = error_code
    if error_message:
        log.error_message = error_message

    db.session.commit()
    return log


def record_inbound_event(garage, event: InboundEvent, *, customer=None) -> CommunicationLog:
    return record_inbound_communication(
        garage=garage,
        channel=event.channel,
        provider=event.provider,
        external_id=event.interaction_id,
        from_address=event.from_address,
        to_address=event.to_address,
        status=event.status,
        body=event.body if event.channel == CHANNEL_WHATSAPP else None,
        customer=customer,
    )


def apply_status_event(event: StatusEvent) -> CommunicationLog | None:
    return update_communication_status(
        provider=event.provider,
        external_id=event.interaction_id,
        status=event.status,
        call_duration_seconds=event.duration_seconds,
        error_code=event.error_code,
        error_message=event.error_message,
    )
