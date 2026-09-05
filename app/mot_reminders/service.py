"""MOT reminder scheduling + delivery.

One module owns:

* deciding whether a vehicle currently has an MOT booking that should suppress
  reminders (:func:`mot_booking_active_for`),
* working out each vehicle's per-stage reminder state for the staff page
  (:func:`compute_reminder_state`),
* actually delivering a reminder and recording the event
  (:func:`record_and_send`) - the single place email/SMS sending happens, used
  by both the automatic worker and the manual "Send reminder" button, and
* the automatic worker body (:func:`send_due_automatic_reminders`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.communications.events import MOT_REMINDER_DUE, emit_event
from app.email import send_email
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.reminder import (
    AUTOMATIC_STAGES,
    STAGE_1,
    STAGE_2,
    STAGE_3,
    STAGE_MANUAL,
    STATUS_SENT,
    STATUS_SKIPPED,
    TRIGGER_AUTOMATIC,
    TRIGGER_MANUAL,
    Reminder,
)
from app.models.vehicle import Vehicle

from .defaults import resolve_mot_reminder_settings, stages_from

# Appointment statuses that mean "this vehicle is booked in / reserved for an
# MOT" - a reminder would be noise. CANCELLED / NO_SHOW are excluded: the MOT
# still needs doing.
SUPPRESSING_APPOINTMENT_STATUSES = (
    "REQUESTED",
    "BOOKED",
    "IN_PROGRESS",
    "ACTION_NEEDED",
    "COMPLETED",
)

# How late after the expiry date a booking still counts as "for this MOT", and
# how far back to look so last year's completed MOT doesn't suppress this year.
_BOOKING_GRACE_DAYS = 14
_CYCLE_LOOKBACK_DAYS = 330

_STAGE_ORDER = (STAGE_1, STAGE_2, STAGE_3)


def _as_utc_bounds(d1: date, d2: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d1, time.min, tzinfo=UTC),
        datetime.combine(d2, time.max, tzinfo=UTC),
    )


def mot_booking_active_for(
    vehicle_id, garage_id, mot_expiry: date | None, session=None
) -> bool:
    """True if ``vehicle_id`` has a non-cancelled MOT appointment dated around
    ``mot_expiry`` (this cycle). Re-evaluated live before every send, never
    cached on the reminder."""
    if mot_expiry is None:
        return False
    session = session or db.session

    start_dt, end_dt = _as_utc_bounds(
        mot_expiry - timedelta(days=_CYCLE_LOOKBACK_DAYS),
        mot_expiry + timedelta(days=_BOOKING_GRACE_DAYS),
    )

    from app.models.appointments.appointment import Appointment

    exists_q = (
        session.query(Appointment.id)
        .join(
            GarageAppointmentType,
            Appointment.appointment_type_id == GarageAppointmentType.id,
        )
        .filter(
            Appointment.garage_id == garage_id,
            Appointment.vehicle_id == vehicle_id,
            Appointment.status.in_(SUPPRESSING_APPOINTMENT_STATUSES),
            GarageAppointmentType.name.ilike("%mot%"),
            Appointment.start_time >= start_dt,
            Appointment.start_time <= end_dt,
        )
    )
    return session.query(exists_q.exists()).scalar()


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def _reminder_body(garage, customer, vehicle, mot_expiry: date) -> str:
    name = customer.first_name or "there"
    veh = " ".join(p for p in (vehicle.make, vehicle.model) if p) or "your vehicle"
    contact_bits = [b for b in (garage.phone, garage.email) if b]
    contact = f"\n\nTo book, contact {garage.name}" + (
        f" on {' / '.join(contact_bits)}." if contact_bits else "."
    )
    return (
        f"Hi {name},\n\n"
        f"The MOT for {veh} ({vehicle.registration_number}) is due to expire on "
        f"{mot_expiry:%d %B %Y}. Please book your MOT test before then to stay "
        f"road-legal.{contact}\n\n"
        f"{garage.name}"
    )


def deliver_reminder(*, garage, customer, vehicle, mot_expiry: date, channel: str):
    """Send the reminder over ``channel``. Returns ``(status, detail)``.

    The only outbound-message call in the reminder feature - automatic and
    manual sends both come through here, so there is no duplicated email/SMS
    logic.
    """
    if channel == "email":
        if not customer.email:
            return STATUS_SKIPPED, "No email address on file for this customer."
        send_email(
            to=customer.email,
            subject=f"MOT reminder - {vehicle.registration_number}",
            body=_reminder_body(garage, customer, vehicle, mot_expiry),
        )
        return STATUS_SENT, f"Emailed {customer.email}."

    # SMS etc. are not wired yet - structured so a channel can be added without
    # touching callers.
    return STATUS_SKIPPED, f"The {channel!r} channel is not available yet."


def default_channel_for(customer) -> str:
    """The reminder channel to use with no explicit choice. Email for now."""
    return "email"


def record_and_send(
    *,
    garage,
    vehicle,
    customer,
    stage: str,
    trigger: str,
    mot_expiry: date | None,
    channel: str | None = None,
    initiated_by_id=None,
    session=None,
    now: datetime | None = None,
) -> Reminder:
    """Deliver one reminder and persist the event row."""
    session = session or db.session
    now = now or datetime.now(UTC)
    channel = channel or default_channel_for(customer)

    status, detail = deliver_reminder(
        garage=garage,
        customer=customer,
        vehicle=vehicle,
        mot_expiry=mot_expiry or vehicle.mot_expiry_date or now.date(),
        channel=channel,
    )

    reminder = Reminder(
        garage_id=garage.id,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        initiated_by_employee_id=initiated_by_id,
        type="MOT",
        channel=channel,
        trigger=trigger,
        stage=stage,
        mot_expiry_date=mot_expiry,
        scheduled_at=now,
        sent_at=now if status == STATUS_SENT else None,
        status=status,
        detail=detail,
    )
    session.add(reminder)
    session.flush()

    # Fires regardless of `status` - "due" means a stage was reached and
    # processed, not that the email specifically went out. A future WhatsApp
    # handler can use that to attempt its own channel even when email was
    # STATUS_SKIPPED (no address on file).
    emit_event(
        MOT_REMINDER_DUE, garage=garage, customer=customer, vehicle=vehicle, reminder=reminder
    )

    return reminder


# --------------------------------------------------------------------------
# Per-vehicle state for the staff page
# --------------------------------------------------------------------------


def _stage_days_map(settings) -> dict[str, tuple[bool, int]]:
    return {
        STAGE_1: (settings.stage1_enabled, settings.stage1_days_before),
        STAGE_2: (settings.stage2_enabled, settings.stage2_days_before),
        STAGE_3: (settings.stage3_enabled, settings.stage3_days_before),
    }


def compute_reminder_state(
    *, vehicle, settings, events: list[Reminder], booking_active: bool, today: date
):
    """Build the reminder view for one vehicle: per-stage state, history,
    last-sent and the genuinely-next scheduled reminder."""
    expiry = vehicle.mot_expiry_date
    expired = expiry is not None and expiry < today
    stage_days = _stage_days_map(settings)

    cycle_events = [e for e in events if e.mot_expiry_date == expiry]
    sent_stage_dates = {
        e.stage: e.sent_at
        for e in cycle_events
        if e.stage in AUTOMATIC_STAGES and e.status in (STATUS_SENT, STATUS_SKIPPED)
    }

    stages = []
    for key in _STAGE_ORDER:
        enabled, days_before = stage_days[key]
        send_on = expiry - timedelta(days=days_before) if expiry else None
        if key in sent_stage_dates:
            state = "sent"
        elif not enabled:
            state = "disabled"
        elif booking_active:
            state = "suppressed"
        elif expired:
            state = "expired"
        else:
            state = "scheduled"
        stages.append(
            {
                "stage": key,
                "days_before": days_before,
                "enabled": enabled,
                "state": state,
                "sent_at": sent_stage_dates.get(key),
                "scheduled_for": (
                    send_on if state == "scheduled" and send_on else None
                ),
            }
        )

    sent_events = [e for e in events if e.sent_at is not None]
    last_reminder_sent = max((e.sent_at for e in sent_events), default=None)

    next_scheduled = None
    if not booking_active and not expired and expiry is not None:
        candidates = [
            s["scheduled_for"] for s in stages if s["state"] == "scheduled"
        ]
        next_scheduled = min(candidates) if candidates else None

    if booking_active:
        status = "booked"
    elif next_scheduled is not None:
        status = "scheduled"
    elif sent_stage_dates:
        status = "sent"
    elif expired:
        status = "expired"
    else:
        status = "not_scheduled"

    history = [
        {
            "stage": e.stage,
            "trigger": e.trigger,
            "channel": e.channel,
            "status": e.status,
            "sent_at": e.sent_at,
            "scheduled_at": e.scheduled_at,
            "detail": e.detail,
            "initiated_by": getattr(e, "_initiated_by_name", None),
        }
        for e in sorted(
            events, key=lambda e: e.sent_at or e.scheduled_at, reverse=True
        )
    ]

    return {
        "reminder_status": status,
        "booking_active": booking_active,
        "last_reminder_sent": last_reminder_sent,
        "next_reminder_scheduled": next_scheduled,
        "stages": stages,
        "history": history,
    }


# --------------------------------------------------------------------------
# Automatic worker
# --------------------------------------------------------------------------


def send_due_automatic_reminders(
    *, session=None, garage_id=None, now: datetime | None = None
) -> list[Reminder]:
    """Send every enabled automatic stage that is due and not already sent for
    the current expiry cycle. Idempotent: safe to run as often as you like -
    a stage that has already been sent for a cycle is skipped."""
    session = session or db.session
    now = now or datetime.now(UTC)
    today = now.date()

    q = Vehicle.query.filter(Vehicle.mot_expiry_date.isnot(None))
    if garage_id is not None:
        q = q.filter(Vehicle.garage_id == garage_id)
    vehicles = q.all()

    customers = {c.id: c for c in Customer.query.all()}
    created: list[Reminder] = []

    for vehicle in vehicles:
        expiry = vehicle.mot_expiry_date
        if expiry < today:  # pre-expiry sequence only
            continue
        if mot_booking_active_for(vehicle.id, vehicle.garage_id, expiry, session):
            continue

        settings = resolve_mot_reminder_settings(vehicle.garage_id, session)
        already = {
            r.stage
            for r in Reminder.query.filter(
                Reminder.vehicle_id == vehicle.id,
                Reminder.mot_expiry_date == expiry,
                Reminder.type == "MOT",
                Reminder.stage.in_(AUTOMATIC_STAGES),
                Reminder.status.in_((STATUS_SENT, STATUS_SKIPPED)),
            ).all()
        }
        customer = customers.get(vehicle.customer_id)
        if customer is None:
            continue

        for stage_key, days_before in stages_from(settings):
            if stage_key in already:
                continue
            if today >= expiry - timedelta(days=days_before):
                created.append(
                    record_and_send(
                        garage=vehicle.garage,
                        vehicle=vehicle,
                        customer=customer,
                        stage=stage_key,
                        trigger=TRIGGER_AUTOMATIC,
                        mot_expiry=expiry,
                        session=session,
                        now=now,
                    )
                )
                already.add(stage_key)

    if created:
        session.commit()
    return created


def send_manual_reminder(
    *, vehicle, garage, initiated_by_id, channel: str | None = None, session=None
) -> Reminder:
    """The manual "Send reminder" action. Booking checks live in the route so
    it can 409 before we get here (unless the caller acknowledged the booking)."""
    session = session or db.session
    customer = session.get(Customer, vehicle.customer_id)
    return record_and_send(
        garage=garage,
        vehicle=vehicle,
        customer=customer,
        stage=STAGE_MANUAL,
        trigger=TRIGGER_MANUAL,
        mot_expiry=vehicle.mot_expiry_date,
        channel=channel,
        initiated_by_id=initiated_by_id,
        session=session,
    )


def attach_initiator_names(events: list[Reminder]) -> None:
    """Populate ``_initiated_by_name`` on each event (one query, not N)."""
    ids = {e.initiated_by_employee_id for e in events if e.initiated_by_employee_id}
    names: dict = {}
    if ids:
        for emp in Employee.query.filter(Employee.id.in_(ids)).all():
            full = " ".join(p for p in (emp.first_name, emp.last_name) if p)
            names[emp.id] = full or emp.email
    for e in events:
        e._initiated_by_name = names.get(e.initiated_by_employee_id)
