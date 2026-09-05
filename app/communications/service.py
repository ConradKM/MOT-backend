"""Central Twilio/communications service layer.

Every Twilio SDK call in this codebase happens here (or in
``app/communications/client.py``, which this module uses) - never in a route,
a webhook handler, or the booking/appointment/reminder code. Every public
function here always returns a :class:`CommunicationLog` (never raises for a
Twilio-side failure) so callers never need special-case error handling around
"did this actually send" - they read the row's ``status`` if they care.
"""

from __future__ import annotations

import logging

from twilio.base.exceptions import TwilioRestException

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    STATUS_SKIPPED_NOT_CONFIGURED,
    CommunicationLog,
)
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile

from .client import get_twilio_client_for_garage
from .config import garage_communications_enabled, is_twilio_configured

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
    """Normalize a Twilio (or any other) send-time exception into the
    (error_code, error_message) pair stored on the log row."""
    if isinstance(exc, TwilioRestException):
        return (str(exc.code) if exc.code is not None else None, exc.msg)
    return None, str(exc)


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
        external_provider="twilio",
        external_id=None,
        from_address=from_address,
        to_address=to_address,
        status=STATUS_SKIPPED_NOT_CONFIGURED,
        trigger_event=trigger_event,
        body=body,
        error_message=reason,
        **_related_ids(customer, appointment, booking_request),
    )


def find_customer_by_phone(garage, phone: str | None):
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

    return Customer.query.filter_by(garage_id=garage.id, phone=phone).first()


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
    skip_kwargs = {
        "garage": garage,
        "channel": CHANNEL_WHATSAPP,
        "direction": DIRECTION_OUTBOUND,
        "to_address": to,
        "body": body,
        "trigger_event": trigger_event,
        "customer": customer,
        "appointment": appointment,
        "booking_request": booking_request,
    }

    if not is_twilio_configured():
        return _skip(**skip_kwargs, reason="Twilio is not configured for this deployment.")
    if not garage_communications_enabled(garage):
        return _skip(**skip_kwargs, reason="Communications are not enabled for this garage.")
    if not (settings.whatsapp_sender or settings.messaging_service_sid):
        return _skip(**skip_kwargs, reason="No WhatsApp sender configured for this garage.")

    try:
        to_e164 = normalize_uk_mobile(to)
    except InvalidPhoneNumberError as exc:
        return _skip(**skip_kwargs, reason=f"Invalid destination number: {exc}")

    to_address = f"whatsapp:{to_e164}"
    client = get_twilio_client_for_garage(garage)
    assert client is not None  # guaranteed by the is_twilio_configured() check above

    send_kwargs: dict = {"to": to_address, "body": body}
    if settings.messaging_service_sid:
        send_kwargs["messaging_service_sid"] = settings.messaging_service_sid
    else:
        send_kwargs["from_"] = settings.whatsapp_sender

    try:
        message = client.messages.create(**send_kwargs)
    except Exception as exc:  # noqa: BLE001 - a send must never raise; recorded as FAILED below
        error_code, error_message = _failure_fields(exc)
        return _create_log(
            garage_id=garage.id,
            channel=CHANNEL_WHATSAPP,
            direction=DIRECTION_OUTBOUND,
            external_provider="twilio",
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
        external_provider="twilio",
        external_id=message.sid,
        from_address=settings.whatsapp_sender,
        to_address=to_address,
        status=message.status,
        trigger_event=trigger_event,
        body=body,
        **_related_ids(customer, appointment, booking_request),
    )


def initiate_voice_call(
    *,
    garage,
    to: str,
    twiml_url: str,
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
    skip_kwargs = {
        "garage": garage,
        "channel": CHANNEL_VOICE,
        "direction": DIRECTION_OUTBOUND,
        "to_address": to,
        "trigger_event": trigger_event,
        "customer": customer,
        "appointment": appointment,
        "booking_request": booking_request,
    }

    if not is_twilio_configured():
        return _skip(**skip_kwargs, reason="Twilio is not configured for this deployment.")
    if not garage_communications_enabled(garage):
        return _skip(**skip_kwargs, reason="Communications are not enabled for this garage.")
    if not settings.voice_phone_number:
        return _skip(**skip_kwargs, reason="No voice number configured for this garage.")

    try:
        to_e164 = normalize_uk_mobile(to)
    except InvalidPhoneNumberError as exc:
        return _skip(**skip_kwargs, reason=f"Invalid destination number: {exc}")

    client = get_twilio_client_for_garage(garage)
    assert client is not None  # guaranteed by the is_twilio_configured() check above

    try:
        call = client.calls.create(from_=settings.voice_phone_number, to=to_e164, url=twiml_url)
    except Exception as exc:  # noqa: BLE001 - a send must never raise; recorded as FAILED below
        error_code, error_message = _failure_fields(exc)
        return _create_log(
            garage_id=garage.id,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_OUTBOUND,
            external_provider="twilio",
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
        external_provider="twilio",
        external_id=call.sid,
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
    customer=None,
) -> CommunicationLog:
    """Log an inbound call/message a webhook just received. Always succeeds -
    there is no "send" step to fail here, only a record to keep."""
    return _create_log(
        garage_id=garage.id,
        channel=channel,
        direction=DIRECTION_INBOUND,
        external_provider="twilio",
        external_id=external_id,
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

    log = CommunicationLog.query.filter_by(external_id=external_id).first()
    if log is None:
        logger.warning(
            "[communications] status callback for unknown external_id=%s", external_id
        )
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
