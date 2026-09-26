"""Server-side availability calculation for the public booking calendar.

Everything the customer-facing calendar shows is computed here from real data:
the garage's ``garage_schedule_settings`` / ``garage_opening_hours`` /
``garage_schedule_exceptions`` rows (or the in-code defaults when a garage has
none), its live non-cancelled ``appointments``, and its still-PENDING or
AWAITING_PAYMENT ``booking_requests`` (the latter is a deposit payment hold -
see app/payments/service.py), and its ``walkin_reserved_windows`` - bays the
business has withheld from public booking so walk-ins aren't starved (see
app/models/queueing/reserved_window.py). Nothing is mocked or hard-coded.

Timezone note: like the rest of the codebase (see
app/appointments/routes.py::_day_bounds and
app/booking_requests/routes.py::_resolve_appointment_slot) wall-clock slot times
are combined with ``tzinfo=UTC``. Garages are assumed to operate in UTC for now;
revisiting this needs a per-garage timezone column and is out of scope here.
"""

import math
from datetime import date, datetime, time, timedelta

from app.garages.schedule.defaults import DEFAULT_OPENING_HOURS, DEFAULT_SETTINGS
from app.garages.timezones import local_day_for, local_slot_as_utc
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.employee import Employee
from app.models.queueing.reserved_window import WalkInReservedWindow

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

    # Mirrors __slots__ above - set dynamically via setattr in __init__, so
    # mypy needs these declared here to know the attributes exist at all.
    capacity_per_slot: int | None
    default_appointment_minutes: int
    limited_threshold_ratio: float
    max_advance_days: int
    min_lead_time_hours: int
    slot_interval_minutes: int

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
    rows = {oh.weekday: (oh.opens_at, oh.closes_at, oh.is_closed) for oh in garage.opening_hours}
    if not rows:
        return dict(DEFAULT_OPENING_HOURS)
    return {wd: rows.get(wd, DEFAULT_OPENING_HOURS[wd]) for wd in range(7)}


def resolve_exceptions(garage, start_date: date, end_date: date) -> dict:
    return {
        exc.date: exc for exc in garage.schedule_exceptions if start_date <= exc.date <= end_date
    }


def slot_capacity(garage, settings: _Settings) -> int:
    if settings.capacity_per_slot is not None:
        return max(1, settings.capacity_per_slot)
    active_employees: int = Employee.query.filter_by(garage_id=garage.id).count()
    return max(1, active_employees)


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


def open_interval(garage, day: date) -> tuple[datetime, datetime] | None:
    """``day``'s effective opening hours as UTC instants, after one-off
    exceptions - or None when the business is closed that day. The walk-in
    queue's view of "today" (see app/queueing/service.py), resolved through
    exactly the same rules as the booking calendar."""
    hrs = _day_hours(day, resolve_opening_hours(garage), resolve_exceptions(garage, day, day))
    if hrs is None:
        return None
    return local_slot_as_utc(garage, day, hrs[0]), local_slot_as_utc(garage, day, hrs[1])


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _load_day_usage(garage, day: date):
    """Fetch, once per day, the appointments and pending requests that could
    fall on ``day`` so the per-slot loop is pure Python."""
    day_start = local_slot_as_utc(garage, day, time.min)
    day_end = local_slot_as_utc(garage, day, time.max)
    appointments = Appointment.query.filter(
        Appointment.garage_id == garage.id,
        Appointment.status != "CANCELLED",
        Appointment.start_time <= day_end,
        Appointment.end_time >= day_start,
    ).all()
    pending = BookingRequest.query.filter(
        BookingRequest.garage_id == garage.id,
        # AWAITING_PAYMENT reserves the slot exactly like PENDING while a
        # deposit is being paid (see app/payments/service.py) - counted here
        # too so a second customer can't take the slot mid-payment.
        BookingRequest.status.in_(("PENDING", "AWAITING_PAYMENT")),
        BookingRequest.preferred_date == day,
        BookingRequest.preferred_time.isnot(None),
    ).all()
    return appointments, pending


def _load_reserved_windows(garage, day: date) -> list[tuple[datetime, datetime, int]]:
    """``(start, end, reserved_capacity)`` UTC intervals of every walk-in
    window - recurring (by weekday) or one-off (by date) - that applies on
    ``day``."""
    rows = WalkInReservedWindow.query.filter(
        WalkInReservedWindow.garage_id == garage.id,
        (WalkInReservedWindow.weekday == day.weekday()) | (WalkInReservedWindow.date == day),
    ).all()
    return [
        (
            local_slot_as_utc(garage, day, row.starts_at),
            local_slot_as_utc(garage, day, row.ends_at),
            row.reserved_capacity,
        )
        for row in rows
    ]


def _reserved_at(windows, slot_start: datetime, slot_end: datetime) -> int:
    """Bays withheld from public booking anywhere in ``[slot_start,
    slot_end)``. A booking needs its bay for its whole span, so a window
    touching any part of it counts; overlapping windows take the largest
    reservation rather than summing (two "keep one bay free" rules still
    mean one bay)."""
    return max(
        (n for w_start, w_end, n in windows if w_start < slot_end and w_end > slot_start),
        default=0,
    )


def _pending_request_duration(pending_request: BookingRequest, settings: "_Settings") -> int:
    """The duration a PENDING request itself reserves.

    A request's snapshot is authoritative even if an owner later edits the
    catalogue type: otherwise shortening a 90-minute service could release
    the final hour of a customer's live reservation, while lengthening it
    could consume capacity they never requested.  Legacy rows without the
    snapshot retain the current-type/default fallback.
    """
    if pending_request.requested_duration_minutes is not None:
        return pending_request.requested_duration_minutes
    appt_type = pending_request.appointment_type
    if appt_type is not None and appt_type.default_duration_minutes is not None:
        return appt_type.default_duration_minutes
    return settings.default_appointment_minutes


def _slot_usage(
    garage, appointments, pending, slot_start: datetime, duration_min: int, settings: "_Settings"
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
    used = sum(1 for a in appointments if a.start_time < slot_end and a.end_time > slot_start)
    for r in pending:
        p_start = local_slot_as_utc(garage, r.preferred_date, r.preferred_time)
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

    appointments, pending = _load_day_usage(garage, day)
    windows = _load_reserved_windows(garage, day)

    slots = []
    m = _minutes(opens_at)
    close_m = _minutes(closes_at)
    while m + duration <= close_m:
        slot_time = time(m // 60, m % 60)
        slot_start = local_slot_as_utc(garage, day, slot_time)
        if slot_start >= lead_cutoff:
            used = _slot_usage(garage, appointments, pending, slot_start, duration, settings)
            slot_end = slot_start + timedelta(minutes=duration)
            remaining = capacity - _reserved_at(windows, slot_start, slot_end) - used
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


def day_summary(
    garage, day, settings, hours_map, exceptions, now, today, duration_min=None
) -> dict:
    """One day's green/amber/red indicator for the month calendar.

    ``duration_min`` is the selected service's length, and passing it matters
    for correctness rather than just precision: computed at the garage's
    generic slot length, a day can report ``available`` while a 120-minute
    service has nowhere to fit, so the customer picks a green day and is then
    shown no times at all. Falls back to the generic length only when the
    customer genuinely hasn't chosen a service yet.
    """
    weekday = day.weekday()
    if day < today:
        return _summary(day, weekday, False, LEVEL_PAST, 0, 0)
    if _day_hours(day, hours_map, exceptions) is None:
        return _summary(day, weekday, False, LEVEL_CLOSED, 0, 0)

    slots = day_slots(garage, day, settings, hours_map, exceptions, now, duration_min=duration_min)
    total = len(slots)
    open_slots = sum(1 for s in slots if s["status"] in (SLOT_AVAILABLE, SLOT_LIMITED))
    if open_slots == 0:
        level = LEVEL_FULL
    elif any(s["status"] == SLOT_LIMITED for s in slots) or open_slots <= max(1, total // 3):
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


def availability_range(garage, from_date, to_date, now: datetime, appointment_type=None) -> dict:
    """Payload for GET /api/public/<slug>/availability.

    ``appointment_type`` is optional and defaults to the previous, generic
    behaviour - but the booking wizard now asks what the customer is booking
    *before* showing the calendar, so it should almost always be supplied.
    See :func:`day_summary` for why an unqualified day level can lie.
    """
    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    today = local_day_for(garage, now)
    win_start, win_end = booking_window(settings, today)

    start = max(from_date, win_start) if from_date else win_start
    end = min(to_date, win_end) if to_date else win_end
    end = max(end, start)

    exceptions = resolve_exceptions(garage, start, end)
    duration = _type_duration(appointment_type, settings)

    days = []
    cursor = start
    while cursor <= end:
        days.append(
            day_summary(garage, cursor, settings, hours_map, exceptions, now, today, duration)
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


def _type_duration(appointment_type: "GarageAppointmentType | None", settings: _Settings) -> int:
    if appointment_type is not None and appointment_type.default_duration_minutes is not None:
        return appointment_type.default_duration_minutes
    return settings.default_appointment_minutes


def single_day(
    garage, day: date, now: datetime, appointment_type=None, duration_min: int | None = None
) -> dict:
    """Payload for GET /api/public/<slug>/availability/<date>.

    ``appointment_type`` is the service the customer has selected (optional -
    the calendar's date step doesn't have one yet); when given, its own
    duration drives which start times are offered, per
    app/public_booking/availability.py's module docs.
    """
    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    today = local_day_for(garage, now)
    exceptions = resolve_exceptions(garage, day, day)
    duration = (
        duration_min if duration_min is not None else _type_duration(appointment_type, settings)
    )
    # The same duration drives the summary as drives the slots: this endpoint
    # already knows the service, so reporting a generic `level` next to
    # service-specific `slots` would contradict itself within one payload.
    summary = day_summary(garage, day, settings, hours_map, exceptions, now, today, duration)
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
    exclude_appointment_id=None,
    include_walkin_reservations: bool = True,
) -> tuple[int, int]:
    """``(used, capacity)`` for one candidate slot - the same accounting
    :func:`validate_slot` uses for its capacity check, exposed so other
    callers (approving a booking request) can run the identical check.

    ``exclude_request_id`` leaves one PENDING request's own reservation out of
    ``used`` - pass the request being approved so converting its reservation
    into a real appointment isn't double-counted against itself.
    ``exclude_appointment_id`` does the equivalent for a staff reschedule or
    reactivation, so the appointment is not treated as a collision with its
    own current slot.

    The returned capacity is what *public booking* may use: bays withheld by
    a walk-in reserved window are taken off it. Staff scheduling passes
    ``include_walkin_reservations=False`` - an owner deliberately booking
    someone into a walk-in window is overriding their own rule, not being
    bypassed by a customer.
    """
    settings = resolve_settings(garage)
    capacity = slot_capacity(garage, settings)
    if include_walkin_reservations:
        slot_end = slot_start + timedelta(minutes=duration_min)
        # Windows are wall-clock rules, so look them up by the slot's own
        # business-local day - callers pass ``start_time.date()`` (a UTC
        # date), which differs near midnight for non-UTC businesses.
        windows = _load_reserved_windows(garage, local_day_for(garage, slot_start))
        capacity -= _reserved_at(windows, slot_start, slot_end)
    # ``slot_start`` is the instant whose capacity is being checked.  Callers
    # such as staff scheduling naturally have a UTC timestamp and may pass
    # its ``.date()``, which is the previous calendar day for businesses east
    # of UTC shortly after local midnight. Pending requests store their
    # *business-local* preferred_date, so derive that date here instead of
    # trusting the caller's calendar interpretation.
    usage_day = local_day_for(garage, slot_start)
    appointments, pending = _load_day_usage(garage, usage_day)
    if exclude_request_id is not None:
        pending = [p for p in pending if p.id != exclude_request_id]
    if exclude_appointment_id is not None:
        appointments = [a for a in appointments if a.id != exclude_appointment_id]
    used = _slot_usage(garage, appointments, pending, slot_start, duration_min, settings)
    return used, capacity


def validate_slot(
    garage,
    day: date,
    slot_time: time,
    now: datetime,
    appointment_type=None,
    exclude_appointment_id=None,
    duration_min: int | None = None,
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
    today = local_day_for(garage, now)
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
    duration = (
        duration_min if duration_min is not None else _type_duration(appointment_type, settings)
    )
    start_m = _minutes(slot_time)
    if start_m < _minutes(opens_at) or start_m + duration > _minutes(closes_at):
        return "outside_hours"

    slot_start = local_slot_as_utc(garage, day, slot_time)
    if slot_start <= now:
        return "past"
    if slot_start < now + timedelta(hours=settings.min_lead_time_hours):
        return "too_soon"

    used, capacity = slot_capacity_usage(
        garage,
        day,
        slot_start,
        duration,
        exclude_appointment_id=exclude_appointment_id,
    )
    if used >= capacity:
        return "full"

    return None


def check_slot_available(garage, day: date, slot_time: time, now: datetime) -> bool:
    """Back-compat boolean wrapper around :func:`validate_slot`."""
    return validate_slot(garage, day, slot_time, now) is None
