"""Templated transactional email for appointments and customer accounts.

The dedicated email-service layer the booking/appointment/account code talks
to - never Resend calls inline in a route. It renders the Jinja templates
under app/templates/emails/, and logs every attempt to CommunicationLog (the
same table Twilio sends already use, ``channel=EMAIL`` - see
app/models/communications/communication_log.py), so email shows up in the
same per-tenant communications history as WhatsApp/SMS/voice.

Two guarantees every public function here makes, mirroring
app/communications/service.py's contract for Twilio sends:

* It never raises. A booking or account action must never fail because a
  confirmation email couldn't be sent - a ``send_email`` failure is caught,
  logged (never the API key - see app/email/__init__.py), and recorded as a
  FAILED CommunicationLog row instead of propagating.
* It never sends the same email twice for the same trigger. Before sending,
  it checks for an existing SENT row for the same (trigger_event,
  appointment) or (trigger_event, customer) pair and skips if one exists -
  the same "already sent?" guard app/mot_reminders/service.py uses per
  reminder stage/cycle, applied here per appointment/account event instead of
  inventing a separate idempotency-key mechanism.

Every function also derives its own recipient from the domain object it's
given (``customer.email`` / ``appointment.customer.email``) - never from a
caller-supplied address - so there's no way to point one of these at an
arbitrary destination.
"""

from __future__ import annotations

import logging

from flask import current_app, render_template

from app.branding import PLATFORM_NAME_TM
from app.communications.events import (
    ACCOUNT_CREATED,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CREATED,
    APPOINTMENT_RESCHEDULED,
)
from app.email import send_email
from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_EMAIL,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)

logger = logging.getLogger(__name__)

STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"


def _already_sent(*, trigger_event: str, appointment_id=None, customer_id=None) -> bool:
    query = CommunicationLog.query.filter(
        CommunicationLog.channel == CHANNEL_EMAIL,
        CommunicationLog.trigger_event == trigger_event,
        CommunicationLog.status == STATUS_SENT,
    )
    if appointment_id is not None:
        query = query.filter(CommunicationLog.appointment_id == appointment_id)
    elif customer_id is not None:
        query = query.filter(
            CommunicationLog.appointment_id.is_(None),
            CommunicationLog.customer_id == customer_id,
        )
    else:
        return False
    return bool(db.session.query(query.exists()).scalar())


def _log(**fields) -> CommunicationLog:
    log = CommunicationLog(**fields)
    db.session.add(log)
    db.session.commit()
    return log


def _send(
    *,
    garage,
    to: str | None,
    subject: str,
    template: str,
    context: dict,
    trigger_event: str,
    customer=None,
    appointment=None,
) -> CommunicationLog | None:
    """Render ``template`` (+ its .txt companion), send it, and log the
    result. Returns None only when there's nowhere to send it (no email on
    file for this customer) or it was already sent - every other outcome
    (sent, failed) is a logged CommunicationLog row."""
    if not to:
        return None

    appointment_id = appointment.id if appointment is not None else None
    customer_id = customer.id if customer is not None else None

    if _already_sent(
        trigger_event=trigger_event, appointment_id=appointment_id, customer_id=customer_id
    ):
        logger.info(
            "[email] %s already sent for %s - skipping duplicate send.",
            trigger_event,
            appointment_id or customer_id,
        )
        return None

    render_context = {"platform_name_tm": PLATFORM_NAME_TM, "subject": subject, **context}
    html_body = render_template(f"emails/{template}.html", **render_context)
    text_body = render_template(f"emails/{template}.txt", **render_context)

    provider = current_app.config.get("EMAIL_PROVIDER", "console")
    common_fields = {
        "garage_id": garage.id,
        "channel": CHANNEL_EMAIL,
        "direction": DIRECTION_OUTBOUND,
        "external_provider": provider,
        "to_address": to,
        "trigger_event": trigger_event,
        "body": text_body,
        "customer_id": customer_id,
        "appointment_id": appointment_id,
    }

    try:
        send_email(to=to, subject=subject, body=text_body, html_body=html_body)
    except Exception as exc:  # a notification must never break the caller
        logger.exception("[email] send failed for trigger_event=%s", trigger_event)
        return _log(status=STATUS_FAILED, error_message=str(exc), **common_fields)

    return _log(status=STATUS_SENT, **common_fields)


def _vehicle_label(vehicle) -> str | None:
    if vehicle is None:
        return None
    label = " ".join(p for p in (vehicle.make, vehicle.model) if p) or "Vehicle"
    if vehicle.registration_number:
        label = f"{label} ({vehicle.registration_number})"
    return label


def _appointment_context(appointment) -> dict:
    garage = appointment.garage
    customer = appointment.customer
    return {
        "business_name": garage.name,
        "business_phone": garage.phone,
        "business_email": garage.email,
        "business_address": garage.address,
        "first_name": customer.first_name if customer else None,
        "service_name": appointment.appointment_type.name,
        "start_time": appointment.start_time,
        "end_time": appointment.end_time,
        "vehicle_label": _vehicle_label(appointment.vehicle),
        "notes": appointment.notes,
    }


def send_account_created_email(customer) -> CommunicationLog | None:
    """The "you can now sign in" email for CustomerSetPassword - see
    app/customer_auth/routes.py::CustomerSetPassword. Never includes the
    password itself, only the email address it's now tied to."""
    garage = customer.garage
    base_url = (current_app.config.get("APP_BASE_URL") or "").rstrip("/") or None
    return _send(
        garage=garage,
        to=customer.email,
        subject=f"Your {garage.name} account has been created",
        template="account_created",
        context={
            "business_name": garage.name,
            "business_phone": garage.phone,
            "business_email": garage.email,
            "business_address": garage.address,
            "first_name": customer.first_name,
            "email": customer.email,
            "sign_in_url": base_url,
        },
        trigger_event=ACCOUNT_CREATED,
        customer=customer,
    )


def send_appointment_confirmation_email(appointment) -> CommunicationLog | None:
    customer = appointment.customer
    return _send(
        garage=appointment.garage,
        to=customer.email if customer else None,
        subject="Your appointment is confirmed",
        template="appointment_confirmation",
        context=_appointment_context(appointment),
        trigger_event=APPOINTMENT_CREATED,
        customer=customer,
        appointment=appointment,
    )


def send_appointment_changed_email(
    appointment, *, previous_start_time=None, previous_end_time=None
) -> CommunicationLog | None:
    customer = appointment.customer
    context = _appointment_context(appointment)
    context["previous_start_time"] = previous_start_time
    context["previous_end_time"] = previous_end_time
    return _send(
        garage=appointment.garage,
        to=customer.email if customer else None,
        subject="Your appointment has been updated",
        template="appointment_changed",
        context=context,
        trigger_event=APPOINTMENT_RESCHEDULED,
        customer=customer,
        appointment=appointment,
    )


def send_appointment_completed_email(appointment) -> CommunicationLog | None:
    """Summary email once an appointment is marked COMPLETED. The checklist
    is generated live from the appointment's own AppointmentChecklist -
    never a separate/duplicated model - and only items the garage marked
    ``visible_to_customer`` are included, the same filter public_booking
    already applies to checklist templates."""
    customer = appointment.customer
    checklist_items = []
    if appointment.checklist is not None:
        checklist_items = [
            item for item in appointment.checklist.items if item.visible_to_customer
        ]
    context = _appointment_context(appointment)
    context["checklist_items"] = checklist_items
    context["appointment_notes"] = appointment.notes
    return _send(
        garage=appointment.garage,
        to=customer.email if customer else None,
        subject="Your appointment summary",
        template="appointment_completed",
        context=context,
        trigger_event=APPOINTMENT_COMPLETED,
        customer=customer,
        appointment=appointment,
    )
