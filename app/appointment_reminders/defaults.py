"""Defaults + row resolution for :class:`AppointmentReminderSettings`.

Same pattern as ``app.mot_reminders.defaults``: a garage with no settings row
gets a read-only stand-in with sensible defaults rather than every caller
special-casing "no row yet".
"""

from __future__ import annotations

from types import SimpleNamespace

from app.extensions import db
from app.models.appointment_reminder_settings import (
    AppointmentReminderSettings,
    AppointmentReminderTiming,
)

# Sensible out-of-the-box timings: the day before, and shortly before arrival.
DEFAULT_TIMINGS_HOURS: tuple[int, ...] = (24, 2)

MIN_HOURS_BEFORE = 1
MAX_HOURS_BEFORE = 168  # one week - long enough to be useless beyond this

CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"
VALID_CHANNELS = (CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_WHATSAPP)
DEFAULT_CHANNELS: list[str] = [CHANNEL_EMAIL]


def _default_settings_view(garage_id) -> SimpleNamespace:
    """A read-only stand-in for a missing settings row: disabled, with the
    default timings/channels pre-populated so the UI can show what *would* be
    configured without the owner having saved anything yet."""
    return SimpleNamespace(
        id=None,
        garage_id=garage_id,
        enabled=False,
        channels=list(DEFAULT_CHANNELS),
        timings=[
            SimpleNamespace(id=None, hours_before=hours, enabled=True)
            for hours in DEFAULT_TIMINGS_HOURS
        ],
    )


def resolve_appointment_reminder_settings(garage_id, session=None):
    """The garage's settings row, or the default stand-in if none exists yet.
    Never creates a row - see :func:`seed_appointment_reminder_settings` for
    that (called on first write)."""
    session = session or db.session
    row = session.query(AppointmentReminderSettings).filter_by(garage_id=garage_id).first()
    return row if row is not None else _default_settings_view(garage_id)


def seed_appointment_reminder_settings(garage_id, session=None) -> AppointmentReminderSettings:
    """Create the settings row (with default timings) for ``garage_id`` if it
    doesn't already exist. Caller commits."""
    session = session or db.session
    row = session.query(AppointmentReminderSettings).filter_by(garage_id=garage_id).first()
    if row is not None:
        return row  # type: ignore[no-any-return]

    row = AppointmentReminderSettings(garage_id=garage_id, enabled=False, channels=list(DEFAULT_CHANNELS))
    session.add(row)
    session.flush()
    for hours in DEFAULT_TIMINGS_HOURS:
        session.add(AppointmentReminderTiming(settings_id=row.id, hours_before=hours, enabled=True))
    session.flush()
    return row
