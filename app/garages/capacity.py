"""Booked-vs-capacity summary for the staff dashboard.

Reuses the public-booking availability engine (opening hours, one-off
closures, per-slot capacity) so there is one source of truth for a garage's
capacity - no separate system.

Both sides of the ratio are measured in **minutes of scheduling time**, not a
count of appointment rows against a count of theoretical slots. A raw
appointment count is misleading the moment appointment types have different
durations (30 vs 120 minutes) or a garage has more than one employee: a
30-minute oil change and a 2-hour service both used to count as "1 booked",
and the denominator assumed every appointment took exactly the same default
length. Minutes scale correctly with both.
"""

from datetime import UTC, date, datetime, time, timedelta

from app.models.appointments.appointment import Appointment
from app.public_booking.availability import (
    day_open_minutes,
    resolve_exceptions,
    resolve_opening_hours,
    resolve_settings,
    slot_capacity,
)

# green: comfortably free. amber: >= 70% booked. red: at or over capacity.
_AMBER_RATIO = 0.7


def _level(booked_minutes: int, capacity_minutes: int) -> str:
    if capacity_minutes <= 0:
        return "red" if booked_minutes else "green"
    ratio = booked_minutes / capacity_minutes
    if ratio >= 1:
        return "red"
    if ratio >= _AMBER_RATIO:
        return "amber"
    return "green"


def _booked_minutes_between(garage_id, start_date: date, end_date: date) -> int:
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    end = datetime.combine(end_date, time.max, tzinfo=UTC)
    rows = (
        Appointment.query.filter(
            Appointment.garage_id == garage_id,
            Appointment.status != "CANCELLED",
            Appointment.start_time >= start,
            Appointment.start_time <= end,
        )
        .with_entities(Appointment.start_time, Appointment.end_time)
        .all()
    )
    return sum(
        max(0, int((end_time - start_time).total_seconds() // 60))
        for start_time, end_time in rows
    )


def _range_capacity_minutes(
    hours_map, exceptions, start_date, end_date, resource_count
) -> int:
    total = 0
    day = start_date
    while day <= end_date:
        total += day_open_minutes(day, hours_map, exceptions) * resource_count
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
    # The number of parallel resources (normally the employee count) that can
    # each be booked for the garage's full opening hours.
    resource_count = slot_capacity(garage, settings)

    today_exc = resolve_exceptions(garage, today, today)
    today_capacity_minutes = day_open_minutes(today, hours_map, today_exc) * resource_count
    today_booked_minutes = _booked_minutes_between(garage.id, today, today)

    week_exc = resolve_exceptions(garage, week_start, week_end)
    week_capacity_minutes = _range_capacity_minutes(
        hours_map, week_exc, week_start, week_end, resource_count
    )
    week_booked_minutes = _booked_minutes_between(garage.id, week_start, week_end)

    return {
        "today": {
            "date": today,
            "booked_minutes": today_booked_minutes,
            "capacity_minutes": today_capacity_minutes,
            "level": _level(today_booked_minutes, today_capacity_minutes),
        },
        "week": {
            "start": week_start,
            "end": week_end,
            "booked_minutes": week_booked_minutes,
            "capacity_minutes": week_capacity_minutes,
            "level": _level(week_booked_minutes, week_capacity_minutes),
        },
    }
