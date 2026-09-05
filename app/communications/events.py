"""Booking-lifecycle event hooks for future communications.

This module is the seam the task asks for: a clean integration point between
the booking/appointment/reminder code and whatever eventually sends a
WhatsApp/voice notification, without either side knowing about the other.

- The booking/appointment/reminder modules only ever call ``emit_event`` - they
  never import anything from ``app.communications.service`` directly, and
  never change behaviour based on whether communications are configured.
- Nothing subscribes by default. ``emit_event`` with an empty handler list is
  a true no-op (a debug log line, nothing else) - this is what keeps the
  booking flow working identically whether or not Twilio, or any handler at
  all, exists. Registering a handler (e.g. "send a WhatsApp confirmation on
  BOOKING_REQUEST_APPROVED") is exactly the future, incremental work this
  foundation exists to enable, via ``register_handler``.
- A handler that raises never breaks the caller: ``emit_event`` logs and
  swallows, the same way a failed reminder send must never fail the booking
  request approval that triggered it.

Event constants are free strings, not a DB-backed enum, on purpose - the same
choice already made for ``Reminder.type`` / ``CommunicationLog.status``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

logger = logging.getLogger(__name__)

BOOKING_REQUEST_CREATED = "BOOKING_REQUEST_CREATED"
BOOKING_REQUEST_APPROVED = "BOOKING_REQUEST_APPROVED"
BOOKING_REQUEST_REJECTED = "BOOKING_REQUEST_REJECTED"
APPOINTMENT_RESCHEDULED = "APPOINTMENT_RESCHEDULED"
APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
APPOINTMENT_REMINDER_DUE = "APPOINTMENT_REMINDER_DUE"
MOT_REMINDER_DUE = "MOT_REMINDER_DUE"

EVENT_TYPES = (
    BOOKING_REQUEST_CREATED,
    BOOKING_REQUEST_APPROVED,
    BOOKING_REQUEST_REJECTED,
    APPOINTMENT_RESCHEDULED,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_REMINDER_DUE,
    MOT_REMINDER_DUE,
)

EventHandler = Callable[..., None]

_handlers: dict[str, list[EventHandler]] = defaultdict(list)


def register_handler(event_type: str, handler: EventHandler) -> None:
    """Subscribe ``handler`` to ``event_type``. Called at import time by
    whatever future module actually wants to react (e.g. a
    ``app/communications/handlers.py`` that sends WhatsApp confirmations) -
    nothing in this codebase registers one yet."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown communications event type: {event_type!r}")
    _handlers[event_type].append(handler)


def emit_event(event_type: str, *, garage, **context) -> None:
    """Notify every handler registered for ``event_type``. Always a no-op
    when nothing is registered. Never raises - a handler's failure is logged
    and otherwise ignored, so this can be called from the middle of a booking
    transaction without risking it."""
    handlers = _handlers.get(event_type, ())
    if not handlers:
        logger.debug(
            "[communications] event %s for garage %s - no handlers registered",
            event_type,
            getattr(garage, "id", garage),
        )
        return

    for handler in handlers:
        try:
            handler(garage=garage, **context)
        except Exception:
            logger.exception(
                "[communications] handler %r failed for event %s (garage %s)",
                handler,
                event_type,
                getattr(garage, "id", garage),
            )


def _reset_handlers_for_tests() -> None:
    """Test-only: clear every registered handler between tests so one test's
    ``register_handler`` call can't leak into another."""
    _handlers.clear()
