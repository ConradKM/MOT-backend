"""Automated communication rules (Part 23) - the real handlers for the event
bus app/communications/events.py already defines. That module shipped with
zero handlers registered (a deliberate no-op until "future code" wired one
up); this module is that future code, called once from create_app().

Each handler:

1. Loads this garage's automation settings (off by default - see
   models/communications/automation_settings.py) and bails out silently if
   the relevant toggle is off, or there's no phone number to message.
2. Renders the garage's own template (custom or default - templates.py).
3. Sends through the existing communications service
   (app/communications/service.py::send_whatsapp_message) - never Twilio
   directly, and identical whether Twilio is configured or not (a
   SKIPPED_NOT_CONFIGURED row either way, never a silent no-op that looks
   like nothing happened).

None of this ever runs synchronously inside a booking HTTP request in a way
that could fail it - app/communications/events.py::emit_event already
guarantees a handler's exception is logged and swallowed, never propagated.
"""

from __future__ import annotations

from app.communications.events import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_RESCHEDULED,
    BOOKING_REQUEST_APPROVED,
    BOOKING_REQUEST_CREATED,
    BOOKING_REQUEST_REJECTED,
    CALLBACK_REQUESTED,
    MISSED_CALL,
    MOT_REMINDER_DUE,
    register_handler,
)
from app.communications.service import send_whatsapp_message
from app.extensions import db
from app.models.communications.automation_settings import (
    GarageCommunicationAutomationSettings,
)

from . import templates

_SETTINGS_FIELDS = (
    "booking_ack_enabled",
    "booking_confirmation_enabled",
    "reminder_enabled",
    "reminder_hours_before",
    "missed_call_ack_enabled",
    "conversation_automation_enabled",
)


def _automation_settings(garage) -> GarageCommunicationAutomationSettings:
    """This garage's automation settings, or an unsaved, in-code-default row
    when it has none yet (same "optional row, safe fallback" pattern as
    MOTReminderSettings). Built fresh each call rather than a module-level
    singleton - constructing a mapped model at import time risks running
    before every model is registered (SQLAlchemy needs the *whole* mapping
    configured to resolve relationship() string references)."""
    row: GarageCommunicationAutomationSettings | None = (
        GarageCommunicationAutomationSettings.query.filter_by(garage_id=garage.id).first()
    )
    if row is not None:
        return row
    # An unsaved instance's column defaults only apply on INSERT, not on
    # construction - spelled out explicitly here so a garage with no row
    # yet reads the same safe values a real row would have.
    return GarageCommunicationAutomationSettings(
        booking_ack_enabled=True,
        booking_confirmation_enabled=True,
        reminder_enabled=True,
        reminder_hours_before=24,
        missed_call_ack_enabled=False,
        conversation_automation_enabled=False,
    )


def get_automation_settings(garage) -> GarageCommunicationAutomationSettings:
    """Public read access for the Communications API - see
    _automation_settings above for the fallback-row behaviour this wraps."""
    return _automation_settings(garage)


def update_automation_settings(garage, **changes) -> GarageCommunicationAutomationSettings:
    """Persists an owner's edit to their automation toggles - find-or-create
    the real row (a garage's first edit is what actually creates it; before
    that, _automation_settings's in-code defaults are all that exist).
    ``changes`` is whatever subset of _SETTINGS_FIELDS the request included -
    a partial update, same as set_workflow_step's context_updates."""
    row: GarageCommunicationAutomationSettings | None = (
        GarageCommunicationAutomationSettings.query.filter_by(garage_id=garage.id).first()
    )
    if row is None:
        row = GarageCommunicationAutomationSettings(garage_id=garage.id)
        db.session.add(row)
    for field in _SETTINGS_FIELDS:
        if field in changes:
            setattr(row, field, changes[field])
    db.session.commit()
    return row


def is_conversation_automation_enabled(garage) -> bool:
    """Whether inbound messages for this garage should be routed through the
    conversation engine at all (engine.py's caller checks this, not the
    engine itself, so the engine stays usable standalone from the simulator
    regardless of this per-garage switch)."""
    return _automation_settings(garage).conversation_automation_enabled


def _time_suffix(preferred_time) -> str:
    return f" at {preferred_time.strftime('%H:%M')}" if preferred_time else ""


def _appointment_type_name(obj) -> str:
    appointment_type = getattr(obj, "appointment_type", None)
    return appointment_type.name if appointment_type else "your appointment"


def _handle_booking_request_created(garage, booking_request=None, **_context):
    if booking_request is None or not booking_request.customer_phone:
        return
    settings = _automation_settings(garage)
    if not settings.booking_ack_enabled:
        return

    body = templates.render_template(
        garage,
        templates.BOOKING_ACKNOWLEDGEMENT,
        business_name=garage.name,
        appointment_type=_appointment_type_name(booking_request),
        appointment_date=booking_request.preferred_date.strftime("%d %B %Y"),
        appointment_time_suffix=_time_suffix(booking_request.preferred_time),
    )
    send_whatsapp_message(
        garage=garage,
        to=booking_request.customer_phone,
        body=body,
        booking_request=booking_request,
        trigger_event=BOOKING_REQUEST_CREATED,
    )


def _handle_booking_request_approved(garage, booking_request=None, appointment=None, **_context):
    if booking_request is None or not booking_request.customer_phone:
        return
    settings = _automation_settings(garage)
    if not settings.booking_confirmation_enabled:
        return

    body = templates.render_template(
        garage,
        templates.BOOKING_CONFIRMATION,
        business_name=garage.name,
        appointment_type=_appointment_type_name(booking_request),
        appointment_date=appointment.start_time.strftime("%d %B %Y") if appointment else "",
        appointment_time=appointment.start_time.strftime("%H:%M") if appointment else "",
    )
    send_whatsapp_message(
        garage=garage,
        to=booking_request.customer_phone,
        body=body,
        customer=booking_request.customer,
        appointment=appointment,
        booking_request=booking_request,
        trigger_event=BOOKING_REQUEST_APPROVED,
    )


def _handle_booking_request_rejected(garage, booking_request=None, **_context):
    if booking_request is None or not booking_request.customer_phone:
        return
    settings = _automation_settings(garage)
    if not settings.booking_confirmation_enabled:
        return

    body = templates.render_template(
        garage,
        templates.BOOKING_REJECTED,
        business_name=garage.name,
        appointment_type=_appointment_type_name(booking_request),
        appointment_date=booking_request.preferred_date.strftime("%d %B %Y"),
    )
    send_whatsapp_message(
        garage=garage,
        to=booking_request.customer_phone,
        body=body,
        booking_request=booking_request,
        trigger_event=BOOKING_REQUEST_REJECTED,
    )


def _customer_phone(customer) -> str | None:
    return customer.phone if customer and customer.phone else None


def _handle_appointment_cancelled(garage, appointment=None, **_context):
    if appointment is None:
        return
    settings = _automation_settings(garage)
    if not settings.booking_confirmation_enabled:
        return
    phone = _customer_phone(appointment.customer)
    if not phone:
        return

    body = templates.render_template(
        garage,
        templates.APPOINTMENT_CANCELLED,
        business_name=garage.name,
        appointment_date=appointment.start_time.strftime("%d %B %Y"),
    )
    send_whatsapp_message(
        garage=garage,
        to=phone,
        body=body,
        customer=appointment.customer,
        appointment=appointment,
        trigger_event=APPOINTMENT_CANCELLED,
    )


def _handle_appointment_rescheduled(garage, appointment=None, **_context):
    if appointment is None:
        return
    settings = _automation_settings(garage)
    if not settings.booking_confirmation_enabled:
        return
    phone = _customer_phone(appointment.customer)
    if not phone:
        return

    body = templates.render_template(
        garage,
        templates.APPOINTMENT_RESCHEDULED,
        business_name=garage.name,
        appointment_date=appointment.start_time.strftime("%d %B %Y"),
        appointment_time=appointment.start_time.strftime("%H:%M"),
    )
    send_whatsapp_message(
        garage=garage,
        to=phone,
        body=body,
        customer=appointment.customer,
        appointment=appointment,
        trigger_event=APPOINTMENT_RESCHEDULED,
    )


def _handle_mot_reminder_due(garage, customer=None, vehicle=None, reminder=None, **_context):
    # MOT reminders already have their own delivery path (app/mot_reminders/
    # service.py::deliver_reminder, email today). This hook is deliberately
    # inert for now - it exists so a WhatsApp channel can be added to MOT
    # reminders later without touching app/mot_reminders at all, not to
    # duplicate what that module already does.
    return


def _handle_missed_call(garage, communication_log=None, **_context):
    settings = _automation_settings(garage)
    if not settings.missed_call_ack_enabled:
        return
    if communication_log is None or not communication_log.from_address:
        return

    body = templates.render_template(garage, templates.MISSED_CALL_ACK, business_name=garage.name)
    send_whatsapp_message(
        garage=garage,
        to=communication_log.from_address,
        body=body,
        customer=communication_log.customer,
        trigger_event=MISSED_CALL,
    )


def _handle_callback_requested(garage, callback_request=None, **_context):
    # No customer-facing message today - a callback request's whole point is
    # that a human calls them back, not that the bot sends another message.
    # This hook exists so the event is genuinely wired (Part 23 lists it
    # explicitly) and so future behaviour (e.g. an internal staff alert) has
    # somewhere to attach without touching the conversation engine.
    return


def register_default_handlers() -> None:
    """Wires the real automation handlers into the shared event bus. Called
    once from create_app(), and defensively by tests that need them present
    regardless of what an unrelated test's ``_reset_handlers_for_tests``
    call did - safe to call any number of times, since
    ``events.register_handler`` itself is idempotent per (event, handler).
    """
    register_handler(BOOKING_REQUEST_CREATED, _handle_booking_request_created)
    register_handler(BOOKING_REQUEST_APPROVED, _handle_booking_request_approved)
    register_handler(BOOKING_REQUEST_REJECTED, _handle_booking_request_rejected)
    register_handler(APPOINTMENT_CANCELLED, _handle_appointment_cancelled)
    register_handler(APPOINTMENT_RESCHEDULED, _handle_appointment_rescheduled)
    register_handler(MOT_REMINDER_DUE, _handle_mot_reminder_due)
    register_handler(MISSED_CALL, _handle_missed_call)
    register_handler(CALLBACK_REQUESTED, _handle_callback_requested)
