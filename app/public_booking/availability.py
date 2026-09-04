"""Server-side availability calculation for the public booking calendar.

Everything the customer-facing calendar shows is computed here from real data:
the garage's ``garage_schedule_settings`` / ``garage_opening_hours`` /
``garage_schedule_exceptions`` rows (or the in-code defaults when a garage has
none), its live non-cancelled ``appointments``, and its still-PENDING
``booking_requests``. Nothing is mocked or hard-coded.

Timezone note: like the rest of the codebase (see
app/appointments/routes.py::_day_bounds and
app/booking_requests/routes.py::_resolve_appointment_slot) wall-clock slot times
are combined with ``tzinfo=UTC``. Garages are assumed to operate in UTC for now;
revisiting this needs a per-garage timezone column and is out of scope here.
"""

import math
from datetime import UTC, date, datetime, time, timedelta

from app.garages.schedule.defaults import DEFAULT_OPENING_HOURS, DEFAULT_SETTINGS
from app.models.appointments.appointment import Appointment
from app.models.booking_request import BookingRequest
from app.models.employee import Employee

# Levels a day can report - the calendar maps these to its green / amber / red
# (plus text + icon) indicators.
LEVEL_AVAILABLE = "available"
LEVEL_LIMITED = "limited"
LEVEL_FULL = "full"
LEVEL_CLOSED = "closed"
LEVEL_PAST = "past"

# Slot-level statuses.
SLOT_AVAILABLE = "available"
SLOT_LIMITED = "limited"
SLOT_BOOKED = "booked"


class _Settings:
    """Attribute holder so callers don't care whether the values came from a
    ``GarageScheduleSettings`` row or from the defaults."""

    __slots__ = (
        "capacity_per_slot",
        "default_appointment_minutes",
        "limited_threshold_ratio",
        "max_advance_days",
        "min_lead_time_hours",
        "slot_interval_minutes",
    )

    def __init__(self, **kw):
        for key in self.__slots__:
            setattr(self, key, kw[key])


def resolve_settings(garage) -> _Settings:
    row = garage.schedule_settings
    if row is None:
        return _Settings(**DEFAULT_SETTINGS)
    return _Settings(
        slot_interval_minutes=row.slot_interval_minutes,
        default_appointment_minutes=row.default_appointment_minutes,
        min_lead_time_hours=row.min_lead_time_hours,
        max_advance_days=row.max_advance_days,
        capacity_per_slot=row.capacity_per_slot,
        limited_threshold_ratio=float(row.limited_threshold_ratio),
    )


def resolve_opening_hours(garage) -> dict[int, tuple[time, time, bool]]:
    """weekday (0=Mon .. 6=Sun) -> (opens_at, closes_at, is_closed)."""
    rows = {
        oh.weekday: (oh.opens_at, oh.closes_at, oh.is_closed)
        for oh in garage.opening_hours
    }
    if not rows:
        return dict(DEFAULT_OPENING_HOURS)
    return {wd: rows.get(wd, DEFAULT_OPENING_HOURS[wd]) for wd in range(7)}


def resolve_exceptions(garage, start_date: date, end_date: date) -> dict:
    return {
        exc.date: exc
        for exc in garage.schedule_exceptions
        if start_date <= exc.date <= end_date
    }


def slot_capacity(garage, settings: _Settings) -> int:
    if settings.capacity_per_slot is not None:
        return max(1, settings.capacity_per_slot)
    return max(1, Employee.query.filter_by(garage_id=garage.id).count())


def booking_window(settings: _Settings, today: date) -> tuple[date, date]:
    return today, today + timedelta(days=settings.max_advance_days)


def _day_hours(day: date, hours_map, exceptions) -> tuple[time, time] | None:
    """Effective (opens_at, closes_at) for ``day``, or None if closed."""
    exc = exceptions.get(day)
    if exc is not None:
        if exc.is_closed:
            return None
        if exc.opens_at is not None and exc.closes_at is not None:
            return (exc.opens_at, exc.closes_at)
        # A non-closed exception with no explicit hours just means "open as
        # normal" - fall through to the weekday's regular hours.
    opens_at, closes_at, is_closed = hours_map[day.weekday()]
    if is_closed:
        return None
    return (opens_at, closes_at)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _load_day_usage(garage_id, day: date):
    """Fetch, once per day, the appointments and pending requests that could
    fall on ``day`` so the per-slot loop is pure Python."""
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = datetime.combine(day, time.max, tzinfo=UTC)
    appointments = Appointment.query.filter(
        Appointment.garage_id == garage_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time <= day_end,
        Appointment.end_time >= day_start,
    ).all()
    pending = BookingRequest.query.filter(
        BookingRequest.garage_id == garage_id,
        BookingRequest.status == "PENDING",
        BookingRequest.preferred_date == day,
        BookingRequest.preferred_time.isnot(None),
    ).all()
    return appointments, pending


def _pending_request_duration(pending_request, settings: "_Settings") -> int:
    """The duration a PENDING request itself reserves - its selected
    appointment type's current duration; the snapshot taken at submission
    time if that type has since been edited to have none or deleted
    (BookingRequest.appointment_type_id is a nullable FK, so this can
    happen); the garage's generic default otherwise. Matches
    _duration_minutes_for in app/booking_requests/service.py, which the
    review screen uses for the same request."""
    appt_type = pending_request.appointment_type
    if appt_type is not None and appt_type.default_duration_minutes is not None:
        return appt_type.default_duration_minutes
    if pending_request.requested_duration_minutes is not None:
        return pending_request.requested_duration_minutes
    return settings.default_appointment_minutes


def _slot_usage(
    appointments, pending, slot_start: datetime, duration_min: int, settings: "_Settings"
) -> int:
    """How much of a candidate ``[slot_start, slot_start + duration_min)``
    window is already used by real appointments or PENDING requests.

    Each existing item is checked for a genuine interval overlap against its
    *own* full duration - a 90-minute existing booking or pending request
    blocks every candidate slot it overlaps, not just the one starting at its
    exact start time. This is what lets a PENDING request correctly reserve
    its whole span (e.g. 10:00-11:30 for a 90-minute service), not just the
    single instant 10:00.
    """
    slot_end = slot_start + timedelta(minutes=duration_min)
    used = sum(
        1
        for a in appointments
        if a.start_time < slot_end and a.end_time > slot_start
    )
    for r in pending:
        p_start = datetime.combine(r.preferred_date, r.preferred_time, tzinfo=UTC)
        p_end = p_start + timedelta(minutes=_pending_request_duration(r, settings))
        if p_start < slot_end and p_end > slot_start:
            used += 1
    return used


def day_slots(
    garage, day, settings, hours_map, exceptions, now: datetime, duration_min: int | None = None
) -> list[dict]:
    """The bookable slot list for one open day. Empty when the garage is closed
    that day or every slot is inside the lead-time cutoff.

    ``duration_min`` is the *candidate* appointment's length - normally the
    selected appointment type's ``default_duration_minutes`` - so a slot only
    appears when the whole job fits before closing and the whole span is free
    (see ``_slot_usage``). Falls back to the garage's generic default when not
    given, e.g. for the month calendar's day-level indicator, computed before
    the customer has chosen a type at all.
    """
    hrs = _day_hours(day, hours_map, exceptions)
    if hrs is None:
        return []

    opens_at, closes_at = hrs
    capacity = slot_capacity(garage, settings)
    interval = settings.slot_interval_minutes
    duration = duration_min if duration_min is not None else settings.default_appointment_minutes
    # "limited" only makes sense once a slot can hold more than one booking.
    threshold = math.floor(capacity * settings.limited_threshold_ratio)
    lead_cutoff = now + timedelta(hours=settings.min_lead_time_hours)

    appointments, pending = _load_day_usage(garage.id, day)

    slots = []
    m = _minutes(opens_at)
    close_m = _minutes(closes_at)
    while m + duration <= close_m:
        slot_time = time(m // 60, m % 60)
        slot_start = datetime.combine(day, slot_time, tzinfo=UTC)
        if slot_start >= lead_cutoff:
            used = _slot_usage(appointments, pending, slot_start, duration, settings)
            remaining = capacity - used
            if remaining <= 0:
                status = SLOT_BOOKED
            elif 0 < remaining <= threshold:
                status = SLOT_LIMITED
            else:
                status = SLOT_AVAILABLE
            slots.append(
                {
                    "start": slot_time.strftime("%H:%M"),
                    "status": status,
                    "remaining": max(0, remaining),
                    "capacity": capacity,
                }
            )
        m += interval
    return slots


def day_slot_count(day, settings, hours_map, exceptions) -> int:
    """Number of slot start positions that fit in ``day``'s opening hours
    (0 when the garage is closed that day). Ignores lead time - this is the
    day's *maximum* schedule, used by the staff capacity dashboard."""
    hrs = _day_hours(day, hours_map, exceptions)
    if hrs is None:
        return 0
    opens_at, closes_at = hrs
    interval = settings.slot_interval_minutes
    duration = settings.default_appointment_minutes
    close_m = _minutes(closes_at)
    m = _minutes(opens_at)
    count = 0
    while m + duration <= close_m:
        count += 1
        m += interval
    return count


def day_open_minutes(day, hours_map, exceptions) -> int:
    """Minutes the garage is open on ``day`` (0 when closed) - the per-
    resource capacity unit the staff dashboard uses, since it scales
    correctly with appointments of any duration (unlike counting appointment
    *rows* against a fixed slot count - see app/garages/capacity.py)."""
    hrs = _day_hours(day, hours_map, exceptions)
    if hrs is None:
        return 0
    opens_at, closes_at = hrs
    return _minutes(closes_at) - _minutes(opens_at)


def day_summary(garage, day, settings, hours_map, exceptions, now, today) -> dict:
    weekday = day.weekday()
    if day < today:
        return _summary(day, weekday, False, LEVEL_PAST, 0, 0)
    if _day_hours(day, hours_map, exceptions) is None:
        return _summary(day, weekday, False, LEVEL_CLOSED, 0, 0)

    slots = day_slots(garage, day, settings, hours_map, exceptions, now)
    total = len(slots)
    open_slots = sum(
        1 for s in slots if s["status"] in (SLOT_AVAILABLE, SLOT_LIMITED)
    )
    if open_slots == 0:
        level = LEVEL_FULL
    elif any(s["status"] == SLOT_LIMITED for s in slots) or open_slots <= max(
        1, total // 3
    ):
        level = LEVEL_LIMITED
    else:
        level = LEVEL_AVAILABLE
    return _summary(day, weekday, True, level, open_slots, total)


def _summary(day, weekday, is_open, level, open_slots, total_slots) -> dict:
    return {
        "date": day,
        "weekday": weekday,
        "is_open": is_open,
        "level": level,
        "open_slots": open_slots,
        "total_slots": total_slots,
    }


def _opening_hours_payload(hours_map) -> list[dict]:
    out = []
    for wd in range(7):
        opens_at, closes_at, is_closed = hours_map[wd]
        out.append(
            {
                "weekday": wd,
                "opens_at": opens_at.strftime("%H:%M"),
                "closes_at": closes_at.strftime("%H:%M"),
                "is_closed": is_closed,
            }
        )
    return out


def availability_range(garage, from_date, to_date, now: datetime) -> dict:
    """Payload for GET /api/public/<slug>/availability."""
    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    today = now.date()
    win_start, win_end = booking_window(settings, today)

    start = max(from_date, win_start) if from_date else win_start
    end = min(to_date, win_end) if to_date else win_end
    end = max(end, start)

    exceptions = resolve_exceptions(garage, start, end)

    days = []
    cursor = start
    while cursor <= end:
        days.append(
            day_summary(garage, cursor, settings, hours_map, exceptions, now, today)
        )
        cursor += timedelta(days=1)

    return {
        "garage": {"slug": garage.slug, "name": garage.name},
        "rules": {
            "slot_interval_minutes": settings.slot_interval_minutes,
            "min_lead_time_hours": settings.min_lead_time_hours,
            "max_advance_days": settings.max_advance_days,
            "booking_window_start": win_start,
            "booking_window_end": win_end,
        },
        "opening_hours": _opening_hours_payload(hours_map),
        "days": days,
    }


def _type_duration(appointment_type, settings: _Settings) -> int:
    if appointment_type is not None and appointment_type.default_duration_minutes is not None:
        return appointment_type.default_duration_minutes
    return settings.default_appointment_minutes


def single_day(garage, day: date, now: datetime, appointment_type=None) -> dict:
    """Payload for GET /api/public/<slug>/availability/<date>.

    ``appointment_type`` is the service the customer has selected (optional -
    the calendar's date step doesn't have one yet); when given, its own
    duration drives which start times are offered, per
    app/public_booking/availability.py's module docs.
    """
    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    today = now.date()
    exceptions = resolve_exceptions(garage, day, day)
    summary = day_summary(garage, day, settings, hours_map, exceptions, now, today)
    duration = _type_duration(appointment_type, settings)
    slots = (
        day_slots(garage, day, settings, hours_map, exceptions, now, duration_min=duration)
        if summary["is_open"]
        else []
    )
    return {
        "date": day,
        "is_open": summary["is_open"],
        "level": summary["level"],
        "slots": slots,
    }


def slot_capacity_usage(
    garage,
    day: date,
    slot_start: datetime,
    duration_min: int,
    exclude_request_id=None,
) -> tuple[int, int]:
    """``(used, capacity)`` for one candidate slot - the same accounting
    :func:`validate_slot` uses for its capacity check, exposed so other
    callers (approving a booking request) can run the identical check.

    ``exclude_request_id`` leaves one PENDING request's own reservation out of
    ``used`` - pass the request being approved so converting its reservation
    into a real appointment isn't double-counted against itself.
    """
    settings = resolve_settings(garage)
    capacity = slot_capacity(garage, settings)
    appointments, pending = _load_day_usage(garage.id, day)
    if exclude_request_id is not None:
        pending = [p for p in pending if p.id != exclude_request_id]
    used = _slot_usage(appointments, pending, slot_start, duration_min, settings)
    return used, capacity


def validate_slot(
    garage, day: date, slot_time: time, now: datetime, appointment_type=None
) -> str | None:
    """Submit-time re-check that ``(day, slot_time)`` is genuinely bookable, by
    the same rules the customer calendar uses. Returns ``None`` when it is, or a
    short reason (``past`` / ``closed`` / ``outside_hours`` / ``too_soon`` /
    ``full`` / ``out_of_window``) so a direct API POST can't bypass the calendar.

    ``appointment_type`` (when the request named one) makes this check use
    the *real* selected duration - a slot that only just fits a 30-minute
    diagnostic but not a 90-minute service must be rejected for the latter.
    """
    settings = resolve_settings(garage)
    today = now.date()
    _, win_end = booking_window(settings, today)

    if day < today:
        return "past"
    if day > win_end:
        return "out_of_window"

    hours_map = resolve_opening_hours(garage)
    exceptions = resolve_exceptions(garage, day, day)
    hrs = _day_hours(day, hours_map, exceptions)
    if hrs is None:
        return "closed"

    opens_at, closes_at = hrs
    duration = _type_duration(appointment_type, settings)
    start_m = _minutes(slot_time)
    if start_m < _minutes(opens_at) or start_m + duration > _minutes(closes_at):
        return "outside_hours"

    slot_start = datetime.combine(day, slot_time, tzinfo=UTC)
    if slot_start <= now:
        return "past"
    if slot_start < now + timedelta(hours=settings.min_lead_time_hours):
        return "too_soon"

    used, capacity = slot_capacity_usage(garage, day, slot_start, duration)
    if used >= capacity:
        return "full"

    return None


def check_slot_available(garage, day: date, slot_time: time, now: datetime) -> bool:
    """Back-compat boolean wrapper around :func:`validate_slot`."""
    return validate_slot(garage, day, slot_time, now) is None
