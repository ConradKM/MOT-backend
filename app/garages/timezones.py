"""Business-wall-clock helpers for booking and scheduling.

UTC remains the persistence and comparison format for timestamp columns.
Opening hours, exceptions and booking-request ``date``/``time`` values are
wall-clock values, however, and must be interpreted in the business's local
timezone before they are compared with those UTC instants.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_BUSINESS_TIMEZONE = "Europe/London"


def timezone_for(garage) -> ZoneInfo:
    """Return a valid IANA timezone for a garage without trusting malformed data."""
    try:
        return ZoneInfo(garage.timezone or DEFAULT_BUSINESS_TIMEZONE)
    except ZoneInfoNotFoundError:
        # The column is platform-managed today, but a safe UK fallback is
        # preferable to turning live booking availability into a 500 if a
        # legacy/manual row is malformed.
        return ZoneInfo(DEFAULT_BUSINESS_TIMEZONE)


def local_slot_as_utc(garage, day: date, slot_time: time) -> datetime:
    """Interpret a configured wall-clock slot and return its UTC instant."""
    return datetime.combine(day, slot_time, tzinfo=timezone_for(garage)).astimezone(UTC)


def local_day_for(garage, instant: datetime) -> date:
    """The business-local calendar day containing an aware instant."""
    return instant.astimezone(timezone_for(garage)).date()
