"""Booked-vs-capacity summary for the staff dashboard.

Reuses the public-booking availability engine (opening hours, slot length,
per-slot capacity, one-off closures) so there is one source of truth for a
garage's capacity - no separate system.
"""

from datetime import UTC, date, datetime, time, timedelta

from app.models.appointments.appointment import Appointment
from app.public_booking.availability import (
    day_slot_count,
    resolve_exceptions,
    resolve_opening_hours,
    resolve_settings,
    slot_capacity,
)

# green: comfortably free. amber: >= 70% booked. red: at or over capacity.
_AMBER_RATIO = 0.7


def _level(booked: int, capacity: int) -> str:
    if capacity <= 0:
        return "red" if booked else "green"
    ratio = booked / capacity
    if ratio >= 1:
        return "red"
    if ratio >= _AMBER_RATIO:
        return "amber"
    return "green"


def _booked_between(garage_id, start_date: date, end_date: date) -> int:
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    end = datetime.combine(end_date, time.max, tzinfo=UTC)
    return Appointment.query.filter(
        Appointment.garage_id == garage_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time >= start,
        Appointment.start_time <= end,
    ).count()


def _range_capacity(settings, hours_map, exceptions, start_date, end_date, per_slot) -> int:
    total = 0
    day = start_date
    while day <= end_date:
        total += day_slot_count(day, settings, hours_map, exceptions) * per_slot
        day += timedelta(days=1)
    return total


def capacity_summary(garage, now: datetime | None = None) -> dict:
    """`{today: {...}, week: {...}}` for GET /api/garage/capacity/summary."""
    now = now or datetime.now(UTC)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)

    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    per_slot = slot_capacity(garage, settings)

    today_exc = resolve_exceptions(garage, today, today)
    today_cap = day_slot_count(today, settings, hours_map, today_exc) * per_slot
    today_booked = _booked_between(garage.id, today, today)

    week_exc = resolve_exceptions(garage, week_start, week_end)
    week_cap = _range_capacity(
        settings, hours_map, week_exc, week_start, week_end, per_slot
    )
    week_booked = _booked_between(garage.id, week_start, week_end)

    return {
        "today": {
            "date": today,
            "booked": today_booked,
            "capacity": today_cap,
            "level": _level(today_booked, today_cap),
        },
        "week": {
            "start": week_start,
            "end": week_end,
            "booked": week_booked,
            "capacity": week_cap,
            "level": _level(week_booked, week_cap),
        },
    }
