"""Wires app/communications/service.py::send_sms_message into the shared
communications event bus (see app/communications/events.py) - the SMS
counterpart to app/email/automation.py, and independent of it: a booking
event fires email and SMS as two separate subscribers, neither depending on
the other's success or configuration.

Every handler here checks ``SMS_NOTIFICATIONS_ENABLED`` first (see
app/config.py) - a second, deployment-level gate on top of
send_sms_message's own provider/garage configuration checks, so shipping
this module can never by itself start texting existing tenants' customers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import current_app

from app.communications.events import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_RESCHEDULED,
    BOOKING_REQUEST_APPROVED,
    BOOKING_REQUEST_CREATED,
    BOOKING_REQUEST_REJECTED,
    MOT_REMINDER_DUE,
    QUEUE_ENTRY_CALLED,
    QUEUE_ENTRY_JOINED,
    register_handler,
)
from app.communications.service import send_sms_message
from app.extensions import db


def _sms_notifications_enabled() -> bool:
    return bool(current_app.config.get("SMS_NOTIFICATIONS_ENABLED"))


def _customer_phone(customer) -> str | None:
    return customer.phone if customer and customer.phone else None


def _handle_booking_request_created(garage, booking_request=None, **_context):
    if not _sms_notifications_enabled() or booking_request is None:
        return
    phone = booking_request.customer_phone
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Hi {booking_request.customer_first_name}, we've received your booking "
            f"request ({booking_request.booking_reference}) with {garage.name}. "
            "We'll be in touch to confirm."
        ),
        booking_request=booking_request,
        trigger_event=BOOKING_REQUEST_CREATED,
    )


def _handle_booking_request_rejected(garage, booking_request=None, **_context):
    if not _sms_notifications_enabled() or booking_request is None:
        return
    phone = booking_request.customer_phone
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Hi {booking_request.customer_first_name}, {garage.name} wasn't able to "
            f"take your booking request ({booking_request.booking_reference}). "
            "Please contact them directly to find another time."
        ),
        booking_request=booking_request,
        trigger_event=BOOKING_REQUEST_REJECTED,
    )


def _handle_booking_request_approved(garage, appointment=None, **_context):
    if not _sms_notifications_enabled() or appointment is None:
        return
    phone = _customer_phone(appointment.customer)
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Your appointment with {garage.name} is confirmed for "
            f"{appointment.start_time:%d %b %Y at %H:%M}."
        ),
        customer=appointment.customer,
        appointment=appointment,
        trigger_event=BOOKING_REQUEST_APPROVED,
    )


def _handle_appointment_rescheduled(
    garage, appointment=None, previous_start_time=None, previous_end_time=None, **_context
):
    if not _sms_notifications_enabled() or appointment is None:
        return
    phone = _customer_phone(appointment.customer)
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Your appointment with {garage.name} has been updated - it's now "
            f"{appointment.start_time:%d %b %Y at %H:%M}."
        ),
        customer=appointment.customer,
        appointment=appointment,
        trigger_event=APPOINTMENT_RESCHEDULED,
    )


def _handle_appointment_cancelled(garage, appointment=None, **_context):
    if not _sms_notifications_enabled() or appointment is None:
        return
    phone = _customer_phone(appointment.customer)
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Your appointment with {garage.name} on "
            f"{appointment.start_time:%d %b %Y at %H:%M} has been cancelled."
        ),
        customer=appointment.customer,
        appointment=appointment,
        trigger_event=APPOINTMENT_CANCELLED,
    )


def _handle_mot_reminder_due(garage, customer=None, vehicle=None, reminder=None, **_context):
    if not _sms_notifications_enabled() or customer is None or vehicle is None:
        return
    phone = _customer_phone(customer)
    if not phone:
        return
    send_sms_message(
        garage=garage,
        to=phone,
        body=(
            f"Reminder from {garage.name}: the MOT for {vehicle.registration_number} "
            "is due soon. Book online or get in touch to arrange it."
        ),
        customer=customer,
        trigger_event=MOT_REMINDER_DUE,
    )


def queue_status_url(garage, status_token: str) -> str:
    """The customer's live queue page. The token rides in the URL *fragment*,
    which browsers never send to a server, so it can't land in access logs
    (the page posts it in a request body - see app/queueing/routes.py)."""
    base = current_app.config["BOOKING_BASE_URL"].rstrip("/")
    return f"{base}/queue/{garage.id}/status#{status_token}"


def _handle_queue_entry_joined(garage, queue_entry=None, status_token=None, **_context):
    # Walk-in texts are opt-in per customer at join time, on top of the
    # deployment-wide gate every SMS handler honours.
    if not _sms_notifications_enabled() or queue_entry is None or not queue_entry.sms_opt_in:
        return
    body = (
        f"Hi {queue_entry.customer_first_name}, you're in the queue at {garage.name} "
        f"(ticket {queue_entry.ticket_number}). We'll text you when it's your turn."
    )
    if status_token:
        body += f" Live position: {queue_status_url(garage, status_token)}"
    send_sms_message(
        garage=garage, to=queue_entry.customer_phone, body=body, trigger_event=QUEUE_ENTRY_JOINED
    )


def _handle_queue_entry_called(garage, queue_entry=None, **_context):
    if not _sms_notifications_enabled() or queue_entry is None or not queue_entry.sms_opt_in:
        return
    if queue_entry.called_sms_sent_at is not None:
        return
    send_sms_message(
        garage=garage,
        to=queue_entry.customer_phone,
        body=(
            f"Hi {queue_entry.customer_first_name}, it's your turn at {garage.name} "
            f"(ticket {queue_entry.ticket_number}) - please come to reception now."
        ),
        trigger_event=QUEUE_ENTRY_CALLED,
    )
    queue_entry.called_sms_sent_at = datetime.now(UTC)
    db.session.commit()


def register_sms_handlers() -> None:
    """Called once from create_app(), same idempotent-registration contract
    as app/email/automation.py::register_email_handlers - safe to call any
    number of times."""
    register_handler(BOOKING_REQUEST_CREATED, _handle_booking_request_created)
    register_handler(BOOKING_REQUEST_APPROVED, _handle_booking_request_approved)
    register_handler(BOOKING_REQUEST_REJECTED, _handle_booking_request_rejected)
    register_handler(APPOINTMENT_RESCHEDULED, _handle_appointment_rescheduled)
    register_handler(APPOINTMENT_CANCELLED, _handle_appointment_cancelled)
    register_handler(MOT_REMINDER_DUE, _handle_mot_reminder_due)
    register_handler(QUEUE_ENTRY_JOINED, _handle_queue_entry_joined)
    register_handler(QUEUE_ENTRY_CALLED, _handle_queue_entry_called)
