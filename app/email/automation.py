"""Wires app/email/service.py into the shared communications event bus (see
app/communications/events.py) - the same seam app/conversation/automation.py
uses to trigger WhatsApp messages. Kept as its own registration function
(called separately from create_app()) since email should fire regardless of
whether a garage has WhatsApp automation configured at all - the two are
independent subscribers to the same events, not one dependent on the other.
"""

from __future__ import annotations

from app.communications.events import (
    ACCOUNT_CREATED,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CREATED,
    APPOINTMENT_RESCHEDULED,
    register_handler,
)

from .service import (
    send_account_created_email,
    send_appointment_changed_email,
    send_appointment_completed_email,
    send_appointment_confirmation_email,
)


def _handle_appointment_created(garage, appointment=None, **_context):
    if appointment is not None:
        send_appointment_confirmation_email(appointment)


def _handle_appointment_rescheduled(
    garage, appointment=None, previous_start_time=None, previous_end_time=None, **_context
):
    if appointment is not None:
        send_appointment_changed_email(
            appointment,
            previous_start_time=previous_start_time,
            previous_end_time=previous_end_time,
        )


def _handle_appointment_completed(garage, appointment=None, **_context):
    if appointment is not None:
        send_appointment_completed_email(appointment)


def _handle_account_created(garage, customer=None, **_context):
    if customer is not None:
        send_account_created_email(customer)


def register_email_handlers() -> None:
    """Called once from create_app(), same idempotent-registration contract
    as app/conversation/automation.py::register_default_handlers - safe to
    call any number of times."""
    register_handler(APPOINTMENT_CREATED, _handle_appointment_created)
    register_handler(APPOINTMENT_RESCHEDULED, _handle_appointment_rescheduled)
    register_handler(APPOINTMENT_COMPLETED, _handle_appointment_completed)
    register_handler(ACCOUNT_CREATED, _handle_account_created)
