"""Wires loyalty into the shared communications event bus (see
app/communications/events.py). This is the entire appointment/queue
integration: one handler on APPOINTMENT_COMPLETED, which both a normal
appointment completion and a queue walk-in's ``complete_service`` already
emit (see app/loyalty/service.py module docstring for why queueing needs no
separate hook)."""

from app.communications.events import APPOINTMENT_COMPLETED, register_handler

from . import service


def _handle_appointment_completed(*, garage, appointment, **_context) -> None:
    service.record_appointment_completed(appointment)


def register_loyalty_handlers() -> None:
    """Called once from create_app(), like the other event-bus wiring
    functions - safe to call any number of times (register_handler is
    idempotent per (event, handler))."""
    register_handler(APPOINTMENT_COMPLETED, _handle_appointment_completed)
