"""Walk-in queue lifecycle and the live snapshot the ETA is computed from.

Everything here is garage-scoped: every query filters on ``garage_id`` and
every transition first takes the same per-garage row lock public booking and
request approval use (``SELECT ... FOR UPDATE`` on ``garages``), so two staff
members pressing "call next" together - or a join racing a timeout sweep -
can't both act on the same person or allocate the same ticket number.

Nothing about position or ETA is stored. Both are recomputed on every read
from the current rows (see :func:`queue_snapshot`), so a cancellation, a call
or an overrunning service is reflected on the very next poll rather than
frozen at join time.

Timeouts need no scheduler: :func:`sweep_queue` runs lazily at the start of
every staff and public queue read (the same pattern as
app/booking_requests/service.py::expire_stale_booking_requests), and
``flask sweep-walkin-queue`` (app/queueing/cli.py) runs it for every garage
for a deployment that wants the auto-skip to happen even when nobody is
looking at the queue.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, NoReturn

from flask_smorest import abort
from sqlalchemy import func

from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.booking_requests.service import resolve_customer_and_vehicle
from app.communications.events import (
    APPOINTMENT_COMPLETED,
    QUEUE_ENTRY_CALLED,
    QUEUE_ENTRY_JOINED,
    emit_event,
)
from app.extensions import db
from app.garages.timezones import local_day_for, local_slot_as_utc
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.queueing.queue_entry import (
    END_CALL_TIMEOUT,
    END_CUSTOMER_CANCELLED,
    END_DAY_ENDED,
    END_STAFF_CANCELLED,
    END_STAFF_NO_SHOW,
    QUEUE_ACTIVE_STATUSES,
    QUEUE_CALLED,
    QUEUE_CANCELLED,
    QUEUE_DONE,
    QUEUE_IN_SERVICE,
    QUEUE_NO_SHOW,
    QUEUE_WAITING,
    QueueEntry,
)
from app.models.queueing.queue_settings import AVERAGE_MODE_MANUAL, GarageQueueSettings
from app.models.vehicle import Vehicle
from app.public_booking.availability import open_interval, resolve_settings, slot_capacity

from .eta import Busy, Estimate, QueuedJob, estimate_queue, ongoing

# Auto-average window and guard rails. The median rather than the mean: one
# appointment left open overnight (end_time never corrected) would drag a
# mean up by hours, while the median shrugs it off.
AUTO_AVERAGE_LOOKBACK_DAYS = 90
AUTO_AVERAGE_MIN_SAMPLES = 5
# Durations outside this band are data-entry noise, not real services.
_AUTO_AVERAGE_BOUNDS_MINUTES = (5, 8 * 60)
# Most-recent samples considered - plenty for a stable median, and keeps the
# per-read query bounded for a busy business.
_AUTO_AVERAGE_SAMPLE_LIMIT = 500

# Appointment statuses that no longer occupy a bay.
_FINISHED_APPOINTMENT_STATUSES = ("CANCELLED", "NO_SHOW", "COMPLETED")

# Why a join can be refused - machine-readable (errors.reason), with the
# customer-facing wording alongside.
REFUSAL_QUEUE_CLOSED = "queue_closed"
REFUSAL_CLOSED_TODAY = "closed_today"
REFUSAL_PAST_CLOSING = "past_closing"
REFUSAL_FULL_FOR_TODAY = "full_for_today"
REFUSAL_MESSAGES = {
    REFUSAL_QUEUE_CLOSED: "The walk-in queue isn't open right now.",
    REFUSAL_CLOSED_TODAY: "We're closed today, so the walk-in queue isn't running.",
    REFUSAL_PAST_CLOSING: "We've closed for today - please come back tomorrow.",
    REFUSAL_FULL_FOR_TODAY: (
        "The queue is full for today - we wouldn't be able to see you before closing. "
        "Please come back tomorrow or book an appointment."
    ),
}


def utcnow() -> datetime:
    """The one clock every queue operation reads - a seam tests can freeze."""
    return datetime.now(UTC)


def _abort(code: int, message: str, reason: str | None = None) -> NoReturn:
    if reason is None:
        abort(code, message=message)
    else:
        abort(code, message=message, errors={"reason": reason})
    # flask-smorest's abort raises, but isn't annotated NoReturn.
    raise AssertionError("unreachable")


def hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def lock_garage(garage_id: uuid.UUID) -> Garage:
    garage: Garage = db.session.query(Garage).filter_by(id=garage_id).with_for_update().one()
    return garage


# ---------------------------------------------------------------------------
# Settings, capacity and service time
# ---------------------------------------------------------------------------


def get_queue_settings(garage_id: uuid.UUID) -> GarageQueueSettings:
    """The garage's settings row, created with defaults on first use (so
    existing tenants need no backfill - same approach as
    app/garages/schedule/routes.py::_ensure_seeded)."""
    row: GarageQueueSettings | None = GarageQueueSettings.query.filter_by(
        garage_id=garage_id
    ).first()
    if row is None:
        row = GarageQueueSettings(garage_id=garage_id, is_open=False, no_show_timeout_minutes=10)
        db.session.add(row)
        db.session.flush()
    return row


def queue_capacity(garage: Garage) -> int:
    """Bays serving customers at once - the booking calendar's own number
    (see app/models/queueing/queue_settings.py for why it is shared)."""
    return slot_capacity(garage, resolve_settings(garage))


def auto_average_minutes(garage_id: uuid.UUID, now: datetime) -> tuple[int | None, int]:
    """``(median minutes, sample size)`` of recently COMPLETED appointments,
    measured as ``end_time - start_time``.

    There is no separately tracked "actual service time" on Appointment, so
    for booked work this is the planned duration unless staff corrected the
    times. Walk-ins are exact: completing one sets its appointment's
    end_time to the real finish (see :func:`complete_service`), so the
    figure sharpens as the queue is used. ``None`` until there are at least
    AUTO_AVERAGE_MIN_SAMPLES usable appointments.
    """
    since = now - timedelta(days=AUTO_AVERAGE_LOOKBACK_DAYS)
    rows = (
        Appointment.query.filter(
            Appointment.garage_id == garage_id,
            Appointment.status == "COMPLETED",
            Appointment.start_time >= since,
            Appointment.start_time <= now,
        )
        .order_by(Appointment.start_time.desc())
        .limit(_AUTO_AVERAGE_SAMPLE_LIMIT)
        .with_entities(Appointment.start_time, Appointment.end_time)
        .all()
    )
    low, high = _AUTO_AVERAGE_BOUNDS_MINUTES
    durations = [
        minutes
        for start, end in rows
        if low <= (minutes := (end - start).total_seconds() / 60) <= high
    ]
    if len(durations) < AUTO_AVERAGE_MIN_SAMPLES:
        return None, len(durations)
    return round(statistics.median(durations)), len(durations)


@dataclass(frozen=True)
class AverageInfo:
    effective_minutes: int
    # "MANUAL", "AUTO" or "DEFAULT" (auto mode without enough history yet,
    # falling back to the schedule's default_appointment_minutes).
    source: str
    auto_minutes: int | None
    auto_sample_size: int


def average_info(garage: Garage, settings: GarageQueueSettings, now: datetime) -> AverageInfo:
    auto_minutes, sample_size = auto_average_minutes(garage.id, now)
    if settings.average_mode == AVERAGE_MODE_MANUAL and settings.manual_average_minutes:
        return AverageInfo(settings.manual_average_minutes, "MANUAL", auto_minutes, sample_size)
    if auto_minutes is not None:
        return AverageInfo(auto_minutes, "AUTO", auto_minutes, sample_size)
    return AverageInfo(
        resolve_settings(garage).default_appointment_minutes, "DEFAULT", None, sample_size
    )


def resolve_service_minutes(
    garage: Garage,
    settings: GarageQueueSettings,
    appointment_type: GarageAppointmentType | None,
    now: datetime,
) -> int:
    """How long a walk-in is expected to take: the chosen service's own
    duration when they picked one (the per-type override), else the garage's
    walk-in average."""
    if appointment_type is not None and appointment_type.default_duration_minutes:
        return appointment_type.default_duration_minutes
    return average_info(garage, settings, now).effective_minutes


# ---------------------------------------------------------------------------
# Snapshot + ETA
# ---------------------------------------------------------------------------


@dataclass
class QueueSnapshot:
    now: datetime
    service_date: date
    capacity: int
    opens_at: datetime | None
    closes_at: datetime | None
    entries: list[QueueEntry]
    appointments: list[Appointment]
    estimates: dict[uuid.UUID, Estimate] = field(default_factory=dict)
    # Where a brand-new walk-in with the default service time would land.
    new_joiner: Estimate | None = None

    @property
    def waiting(self) -> list[QueueEntry]:
        return [e for e in self.entries if e.status == QUEUE_WAITING]


def _today_entries(garage_id: uuid.UUID, service_date) -> list[QueueEntry]:
    rows: list[QueueEntry] = (
        QueueEntry.query.filter_by(garage_id=garage_id, service_date=service_date)
        .order_by(QueueEntry.sort_key, QueueEntry.created_at)
        .all()
    )
    return rows


def _busy_intervals(
    entries: list[QueueEntry], appointments: list[Appointment], now: datetime
) -> list[Busy]:
    busy: list[Busy] = []
    for appt in appointments:
        if appt.status in _FINISHED_APPOINTMENT_STATUSES:
            continue
        if appt.status == "IN_PROGRESS":
            busy.append(ongoing(appt.start_time, appt.end_time, now))
        else:
            busy.append(Busy(appt.start_time, appt.end_time))

    appointment_ids = {a.id for a in appointments}
    for entry in entries:
        minutes = timedelta(minutes=entry.service_minutes)
        if entry.status == QUEUE_CALLED:
            # Called forward: their bay is being held from now.
            busy.append(Busy(now, now + minutes))
        elif entry.status == QUEUE_IN_SERVICE and entry.appointment_id not in appointment_ids:
            # Normally counted through its IN_PROGRESS appointment above;
            # this only covers an appointment staff have since deleted.
            started = entry.started_at or now
            busy.append(ongoing(started, started + minutes, now))
    return busy


def queue_snapshot(garage: Garage, now: datetime | None = None) -> QueueSnapshot:
    """Today's queue plus every appointment competing for the same bays,
    with a fresh estimate for each WAITING entry."""
    now = now or utcnow()
    settings = get_queue_settings(garage.id)
    service_date = local_day_for(garage, now)
    hours = open_interval(garage, service_date)
    opens_at, closes_at = hours if hours is not None else (None, None)
    capacity = queue_capacity(garage)
    entries = _today_entries(garage.id, service_date)

    window_start, window_end = _day_window(garage, service_date)
    appointments: list[Appointment] = (
        Appointment.query.filter(
            Appointment.garage_id == garage.id,
            Appointment.start_time < window_end,
            Appointment.end_time > window_start,
        )
        .order_by(Appointment.start_time)
        .all()
    )

    busy = _busy_intervals(entries, appointments, now)
    waiting = [e for e in entries if e.status == QUEUE_WAITING]
    jobs = [QueuedJob(e.id, e.service_minutes) for e in waiting]
    new_joiner_key = object()
    jobs.append(QueuedJob(new_joiner_key, resolve_service_minutes(garage, settings, None, now)))

    results = estimate_queue(
        now=now,
        capacity=capacity,
        opens_at=opens_at,
        closes_at=closes_at,
        busy=busy,
        queue=jobs,
    )
    snapshot = QueueSnapshot(
        now=now,
        service_date=service_date,
        capacity=capacity,
        opens_at=opens_at,
        closes_at=closes_at,
        entries=entries,
        appointments=appointments,
    )
    for est in results:
        if est.key is new_joiner_key:
            snapshot.new_joiner = est
        else:
            assert isinstance(est.key, uuid.UUID)
            snapshot.estimates[est.key] = est
    return snapshot


def _day_window(garage: Garage, day: date) -> tuple[datetime, datetime]:
    return local_slot_as_utc(garage, day, time.min), local_slot_as_utc(garage, day, time.max)


def join_refusal_reason(
    settings: GarageQueueSettings, snapshot: QueueSnapshot, minutes: int
) -> str | None:
    """Why a new walk-in needing ``minutes`` can't join right now, or None.

    Decision (documented in app/queueing/eta.py): joins stop for the day once
    a new last-in-line walk-in couldn't *finish* before closing. People
    already queued are never removed for it.
    """
    if not settings.is_open:
        return REFUSAL_QUEUE_CLOSED
    if snapshot.opens_at is None or snapshot.closes_at is None:
        return REFUSAL_CLOSED_TODAY
    if snapshot.now >= snapshot.closes_at:
        return REFUSAL_PAST_CLOSING
    jobs = [QueuedJob(e.id, e.service_minutes) for e in snapshot.waiting]
    jobs.append(QueuedJob("new", minutes))
    last = estimate_queue(
        now=snapshot.now,
        capacity=snapshot.capacity,
        opens_at=snapshot.opens_at,
        closes_at=snapshot.closes_at,
        busy=_busy_intervals(snapshot.entries, snapshot.appointments, snapshot.now),
        queue=jobs,
    )[-1]
    if not last.fits_today:
        return REFUSAL_FULL_FOR_TODAY
    return None


def call_expires_at(entry: QueueEntry, settings: GarageQueueSettings) -> datetime | None:
    if entry.status != QUEUE_CALLED or entry.called_at is None:
        return None
    if not settings.no_show_timeout_minutes:
        return None
    return entry.called_at + timedelta(minutes=settings.no_show_timeout_minutes)


# ---------------------------------------------------------------------------
# Sweeps (day rollover + no-show timeout)
# ---------------------------------------------------------------------------


def sweep_queue(garage: Garage, now: datetime | None = None) -> list[QueueEntry]:
    """End yesterday's leftovers and time out uncollected calls.

    Returns the entries this sweep *called* (the auto-skip's replacements),
    whose QUEUE_ENTRY_CALLED events have already been emitted post-commit.
    Cheap when there's nothing to do: two indexed reads, no lock.
    """
    now = now or utcnow()
    settings = get_queue_settings(garage.id)
    today = local_day_for(garage, now)

    stale_q = QueueEntry.query.filter(
        QueueEntry.garage_id == garage.id,
        QueueEntry.service_date < today,
        QueueEntry.status.in_((QUEUE_WAITING, QUEUE_CALLED)),
    )
    timeout = settings.no_show_timeout_minutes
    due_q = None
    if timeout:
        due_q = QueueEntry.query.filter(
            QueueEntry.garage_id == garage.id,
            QueueEntry.status == QUEUE_CALLED,
            QueueEntry.service_date == today,
            QueueEntry.called_at <= now - timedelta(minutes=timeout),
        )
    if stale_q.first() is None and (due_q is None or due_q.first() is None):
        # Nothing due - don't take the lock on every poll.
        db.session.commit()  # persists a freshly created settings row, if any
        return []

    lock_garage(garage.id)
    for entry in stale_q.with_for_update().all():
        _end(entry, QUEUE_CANCELLED, END_DAY_ENDED, now)

    called: list[QueueEntry] = []
    if due_q is not None:
        timed_out = due_q.with_for_update().all()
        for entry in timed_out:
            _end(entry, QUEUE_NO_SHOW, END_CALL_TIMEOUT, now)
        # Auto-skip: each timed-out call hands its bay to the next in line.
        for _ in timed_out:
            nxt = _next_waiting(garage.id, today)
            if nxt is None:
                break
            _mark_called(nxt, now)
            called.append(nxt)
    db.session.commit()
    for entry in called:
        emit_event(QUEUE_ENTRY_CALLED, garage=garage, queue_entry=entry)
    return called


def sweep_all_garages(now: datetime | None = None) -> int:
    """For ``flask sweep-walkin-queue``: sweep every garage that has any
    active entry. Returns how many entries were auto-called."""
    now = now or utcnow()
    garage_ids = [
        gid
        for (gid,) in db.session.query(QueueEntry.garage_id)
        .filter(QueueEntry.status.in_((QUEUE_WAITING, QUEUE_CALLED)))
        .distinct()
        .all()
    ]
    total = 0
    for gid in garage_ids:
        garage = db.session.get(Garage, gid)
        if garage is not None:
            total += len(sweep_queue(garage, now))
    return total


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _end(entry: QueueEntry, status: str, reason: str | None, now: datetime) -> None:
    entry.status = status
    entry.end_reason = reason
    entry.ended_at = now


def _mark_called(entry: QueueEntry, now: datetime) -> None:
    entry.status = QUEUE_CALLED
    entry.called_at = now


def _next_waiting(garage_id: uuid.UUID, service_date) -> QueueEntry | None:
    entry: QueueEntry | None = (
        QueueEntry.query.filter_by(
            garage_id=garage_id, service_date=service_date, status=QUEUE_WAITING
        )
        .order_by(QueueEntry.sort_key, QueueEntry.created_at)
        .with_for_update()
        .first()
    )
    return entry


def get_entry_for_update(garage_id: uuid.UUID, entry_id: uuid.UUID) -> QueueEntry:
    entry: QueueEntry | None = (
        QueueEntry.query.filter_by(id=entry_id, garage_id=garage_id).with_for_update().first()
    )
    if entry is None:
        _abort(404, "Queue entry not found.")
    return entry


def _require_status(entry: QueueEntry, allowed: tuple[str, ...], action: str) -> None:
    if entry.status not in allowed:
        _abort(
            409,
            f"Can't {action} - this customer is {entry.status.lower().replace('_', ' ')}.",
            "invalid_transition",
        )


def join_queue(garage: Garage, data: Mapping[str, Any], now: datetime | None = None):
    """Add a walk-in. Returns ``(entry, raw_token)`` - the raw token exists
    only in this return value and the join text."""
    now = now or utcnow()
    sweep_queue(garage, now)
    lock_garage(garage.id)
    settings = get_queue_settings(garage.id)

    appointment_type = None
    if data.get("appointment_type_id") is not None:
        appointment_type = GarageAppointmentType.query.filter_by(
            id=data["appointment_type_id"], garage_id=garage.id, status="ACTIVE"
        ).first()
        if appointment_type is None:
            _abort(422, "appointment_type_id is not an active service for this business.")

    snapshot = queue_snapshot(garage, now)
    minutes = resolve_service_minutes(garage, settings, appointment_type, now)
    reason = join_refusal_reason(settings, snapshot, minutes)
    if reason is not None:
        _abort(409, REFUSAL_MESSAGES[reason], reason)

    phone = data["customer_phone"]
    if any(
        e.customer_phone == phone and e.status in QUEUE_ACTIVE_STATUSES for e in snapshot.entries
    ):
        # Don't mint a second token for the same person: re-issuing one to
        # whoever knows a phone number would let them take over (and cancel)
        # someone else's place.
        _abort(
            409,
            "This mobile number is already in today's queue. Use the link from when you "
            "joined, or ask at reception.",
            "already_in_queue",
        )

    today = snapshot.service_date
    max_ticket, max_sort = (
        db.session.query(
            func.coalesce(func.max(QueueEntry.ticket_number), 0),
            func.coalesce(func.max(QueueEntry.sort_key), 0),
        )
        .filter(QueueEntry.garage_id == garage.id, QueueEntry.service_date == today)
        .one()
    )
    token = token_urlsafe(24)
    entry = QueueEntry(
        garage_id=garage.id,
        status=QUEUE_WAITING,
        service_date=today,
        ticket_number=max_ticket + 1,
        sort_key=max_sort + 1,
        customer_first_name=data["customer_first_name"].strip(),
        customer_last_name=(data.get("customer_last_name") or "").strip() or None,
        customer_phone=phone,
        sms_opt_in=bool(data.get("sms_opt_in")),
        vehicle_registration=(data.get("vehicle_registration") or "").strip() or None,
        notes=data.get("notes"),
        appointment_type_id=appointment_type.id if appointment_type else None,
        service_minutes=minutes,
        public_token_hash=hash_token(token),
    )
    db.session.add(entry)
    db.session.commit()
    emit_event(QUEUE_ENTRY_JOINED, garage=garage, queue_entry=entry, status_token=token)
    return entry, token


def call_entry(garage: Garage, entry_id: uuid.UUID | None, now: datetime | None = None):
    """Call a specific WAITING entry forward, or - with ``entry_id=None`` -
    whoever is first in line."""
    now = now or utcnow()
    lock_garage(garage.id)
    if entry_id is None:
        entry = _next_waiting(garage.id, local_day_for(garage, now))
        if entry is None:
            _abort(409, "Nobody is waiting.", "queue_empty")
    else:
        entry = get_entry_for_update(garage.id, entry_id)
        _require_status(entry, (QUEUE_WAITING,), "call them")
    _mark_called(entry, now)
    db.session.commit()
    emit_event(QUEUE_ENTRY_CALLED, garage=garage, queue_entry=entry)
    return entry


def _find_customer(garage_id: uuid.UUID, phone: str) -> Customer | None:
    customer: Customer | None = (
        Customer.query.filter_by(garage_id=garage_id, phone=phone)
        .order_by(Customer.is_active.desc(), Customer.created_at)
        .first()
    )
    return customer


def _registration_is_someone_elses(
    garage_id: uuid.UUID, registration: str | None, customer: Customer | None
) -> bool:
    if not registration:
        return False
    reg = registration.strip().upper().replace(" ", "")
    vehicle = Vehicle.query.filter_by(garage_id=garage_id, registration_number=reg).first()
    return vehicle is not None and (customer is None or vehicle.customer_id != customer.id)


def _pick_employee(
    garage_id: uuid.UUID, acting: Employee, start: datetime, end: datetime
) -> Employee:
    """Who the walk-in's appointment is recorded against, when staff didn't
    say: the person checking them in if they're free, else the first free
    active colleague, else still the person checking them in. Checking in a
    customer who is standing at the desk is never blocked on bookkeeping -
    an explicit ``employee_id`` is always available to correct it."""
    colleagues: list[Employee] = (
        Employee.query.filter(
            Employee.garage_id == garage_id,
            Employee.is_active.is_(True),
            Employee.id != acting.id,
        )
        .order_by(Employee.id)
        .all()
    )
    for employee in [acting, *colleagues]:
        if not employee.is_active:
            continue
        clash = Appointment.query.filter(
            Appointment.employee_id == employee.id,
            Appointment.status.notin_(_FINISHED_APPOINTMENT_STATUSES),
            Appointment.start_time < end,
            Appointment.end_time > start,
        ).first()
        if clash is None:
            return employee
    return acting


def start_service(
    acting: Employee,
    entry_id: uuid.UUID,
    data: Mapping[str, Any],
    now: datetime | None = None,
) -> QueueEntry:
    """The customer has come forward: promote the entry to a real
    ``Appointment`` (status IN_PROGRESS, starting now) and mark it IN_SERVICE.

    This is the only place a walk-in becomes an appointment - see
    app/models/queueing/queue_entry.py for why it isn't done at call time.
    Accepts WAITING as well as CALLED so a customer already at the counter
    can be taken straight in. No capacity check: the customer is physically
    being served, which is a fact to record, not a request to approve.
    APPOINTMENT_CREATED is deliberately not emitted - a "your appointment is
    booked" message to someone already in the bay is noise.
    """
    now = now or utcnow()
    garage_id = acting.garage_id
    lock_garage(garage_id)
    entry = get_entry_for_update(garage_id, entry_id)
    _require_status(entry, (QUEUE_WAITING, QUEUE_CALLED), "start their service")
    settings = get_queue_settings(garage_id)

    appointment_type = _resolve_appointment_type(garage_id, entry, settings, data)
    minutes = entry.service_minutes
    if data.get("appointment_type_id") and appointment_type.default_duration_minutes:
        # Staff changed what the job is - its own duration is now the best guess.
        minutes = appointment_type.default_duration_minutes
    start, end = now, now + timedelta(minutes=minutes)

    if data.get("employee_id") is not None:
        employee = Employee.query.filter_by(id=data["employee_id"], garage_id=garage_id).first()
        if employee is None:
            _abort(422, "employee_id does not belong to your business.")
        if not employee.is_active:
            _abort(422, "This employee's account is deactivated.")
    else:
        employee = _pick_employee(garage_id, acting, start, end)

    existing = _find_customer(garage_id, entry.customer_phone)
    registration = entry.vehicle_registration
    if _registration_is_someone_elses(garage_id, registration, existing):
        # Don't block check-in on a registration clash - record the job
        # without the vehicle and keep the typed registration in the notes.
        registration = None
    customer, vehicle = resolve_customer_and_vehicle(
        garage_id,
        customer_id=existing.id if existing else None,
        customer_email=None,
        first_name=entry.customer_first_name,
        last_name=entry.customer_last_name or "",
        phone=entry.customer_phone,
        vehicle_registration=registration,
    )

    notes = [f"Walk-in, ticket {entry.ticket_number}."]
    if entry.vehicle_registration and registration is None:
        notes.append(f"Registration given: {entry.vehicle_registration}.")
    if entry.notes:
        notes.append(entry.notes)
    appointment = Appointment(
        garage_id=garage_id,
        employee_id=employee.id,
        customer_id=customer.id,
        vehicle_id=vehicle.id if vehicle else None,
        appointment_type_id=appointment_type.id,
        start_time=start,
        end_time=end,
        status="IN_PROGRESS",
        notes=" ".join(notes),
        price_at_booking=appointment_type.base_price,
        appointment_type_name_at_booking=appointment_type.name,
    )
    db.session.add(appointment)
    db.session.flush()
    snapshot_checklist_for_appointment(appointment)

    entry.status = QUEUE_IN_SERVICE
    entry.started_at = now
    entry.called_at = entry.called_at or now
    entry.appointment_id = appointment.id
    entry.appointment_type_id = appointment_type.id
    entry.service_minutes = minutes
    db.session.commit()
    return entry


def _resolve_appointment_type(
    garage_id: uuid.UUID,
    entry: QueueEntry,
    settings: GarageQueueSettings,
    data: Mapping[str, Any],
) -> GarageAppointmentType:
    """Staff's choice, else what the customer picked, else the queue's
    default service, else the business's first active service - Appointment
    requires one, and a walk-in who didn't know what they needed is normal."""
    candidates = [
        data.get("appointment_type_id"),
        entry.appointment_type_id,
        settings.default_appointment_type_id,
    ]
    for type_id in candidates:
        if type_id is None:
            continue
        found: GarageAppointmentType | None = GarageAppointmentType.query.filter_by(
            id=type_id, garage_id=garage_id
        ).first()
        if found is not None and found.status == "ACTIVE":
            return found
        if type_id == data.get("appointment_type_id"):
            _abort(422, "appointment_type_id is not an active service for this business.")
    fallback: GarageAppointmentType | None = (
        GarageAppointmentType.query.filter_by(garage_id=garage_id, status="ACTIVE")
        .order_by(GarageAppointmentType.order, GarageAppointmentType.name)
        .first()
    )
    if fallback is None:
        _abort(
            422,
            "Add at least one active service before checking walk-ins in - "
            "their appointment needs one.",
            "no_appointment_type",
        )
    return fallback


def complete_service(garage: Garage, entry_id: uuid.UUID, now: datetime | None = None):
    """IN_SERVICE -> DONE. The appointment's end_time becomes the *actual*
    finish, which is what makes walk-ins exact samples for the auto-average."""
    now = now or utcnow()
    lock_garage(garage.id)
    entry = get_entry_for_update(garage.id, entry_id)
    _require_status(entry, (QUEUE_IN_SERVICE,), "complete their service")
    _end(entry, QUEUE_DONE, None, now)
    appointment = entry.appointment
    completed = False
    if appointment is not None and appointment.status != "COMPLETED":
        appointment.status = "COMPLETED"
        appointment.end_time = max(now, appointment.start_time + timedelta(minutes=1))
        completed = True
    db.session.commit()
    if completed:
        emit_event(APPOINTMENT_COMPLETED, garage=garage, appointment=appointment)
    return entry


def mark_no_show(garage: Garage, entry_id: uuid.UUID, now: datetime | None = None):
    now = now or utcnow()
    lock_garage(garage.id)
    entry = get_entry_for_update(garage.id, entry_id)
    _require_status(entry, (QUEUE_WAITING, QUEUE_CALLED), "mark them as a no-show")
    _end(entry, QUEUE_NO_SHOW, END_STAFF_NO_SHOW, now)
    db.session.commit()
    return entry


def cancel_entry(garage: Garage, entry: QueueEntry, *, by_customer: bool, now=None) -> QueueEntry:
    """Leave the queue. Only before service starts - once IN_SERVICE the
    appointment exists and is completed (or cancelled) through staff tools."""
    now = now or utcnow()
    _require_status(entry, (QUEUE_WAITING, QUEUE_CALLED), "cancel")
    _end(
        entry,
        QUEUE_CANCELLED,
        END_CUSTOMER_CANCELLED if by_customer else END_STAFF_CANCELLED,
        now,
    )
    db.session.commit()
    return entry


def reorder_waiting(garage: Garage, entry_ids: list[uuid.UUID], now=None) -> None:
    """Staff drag-reorder. ``entry_ids`` must be exactly today's WAITING
    entries - a stale screen (someone joined or was called meanwhile) gets a
    409 to refresh rather than silently dropping someone to the back."""
    now = now or utcnow()
    lock_garage(garage.id)
    waiting = (
        QueueEntry.query.filter_by(
            garage_id=garage.id,
            service_date=local_day_for(garage, now),
            status=QUEUE_WAITING,
        )
        .with_for_update()
        .all()
    )
    by_id = {e.id: e for e in waiting}
    if len(entry_ids) != len(set(entry_ids)) or set(entry_ids) != set(by_id):
        _abort(
            409,
            "The queue changed while you were reordering it - refresh and try again.",
            "stale_order",
        )
    base = min((e.sort_key for e in waiting), default=0)
    for offset, entry_id in enumerate(entry_ids):
        by_id[entry_id].sort_key = base + offset
    db.session.commit()
