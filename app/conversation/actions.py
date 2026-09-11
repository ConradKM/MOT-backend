"""The safe domain action/"tool" layer conversational input is allowed to
trigger (Part 33/34 of the brief).

Even a future LLM-backed intent resolver may only ever call these functions
with already-validated, typed arguments - never raw customer text, never a
raw id it invented. Every function here is the *only* place app/conversation
touches booking/customer/vehicle/availability models directly; nothing in
intents.py, datetime_parsing.py, appointment_matching.py, workflows.py or
engine.py imports them directly. Each function is tenant-scoped by taking an
explicit, already-resolved ``garage`` and only ever reading/writing that
garage's own rows - conversational input can select *which* of a garage's
own records to act on, never *whose* garage to act on.

None of these raise Flask/HTTP aborts - they return plain values (``None``,
``(ok, reason)`` tuples) so they work identically from a real webhook, the
development simulator, or a unit test, none of which are Flask request
handlers themselves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.booking_requests.reference import unique_booking_reference
from app.communications.events import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_RESCHEDULED,
    BOOKING_REQUEST_CREATED,
    CALLBACK_REQUESTED,
    emit_event,
)
from app.communications.service import find_customer_by_phone
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.conversation.callback_request import CallbackRequest
from app.models.customer import Customer
from app.models.vehicle import Vehicle
from app.public_booking import availability

# Re-exported for callers that only need "is this a real, currently
# rejectable reason" without importing the availability module themselves.
SLOT_REASONS = ("past", "closed", "outside_hours", "too_soon", "full", "out_of_window")


def get_appointment_types(garage) -> list[GarageAppointmentType]:
    """Every ACTIVE appointment type this specific garage currently offers -
    never a hard-coded list (Part 5)."""
    return [t for t in garage.appointment_types if t.status == "ACTIVE"]


def get_availability_for_day(garage, day: date, *, appointment_type=None, now=None) -> dict:
    """Real, live slots for one day - exactly what the public booking
    calendar shows (app/public_booking/availability.py::single_day). Never
    duplicated/re-derived - this *is* the availability service."""
    now = now or datetime.now(UTC)
    return availability.single_day(garage, day, now, appointment_type=appointment_type)


def latest_bookable_day(garage, *, now=None) -> date:
    """The furthest-ahead date this garage currently accepts bookings for -
    the same ``max_advance_days`` window the public booking page enforces
    (app/public_booking/availability.py::booking_window). Used to give a
    clear "we can only book up to <date>" instead of a vague re-ask."""
    now = now or datetime.now(UTC)
    _, window_end = availability.booking_window(availability.resolve_settings(garage), now.date())
    return window_end


def find_next_available_days(
    garage, *, appointment_type=None, start_day: date, now=None, limit: int = 5
) -> list[date]:
    """The next ``limit`` open-with-at-least-one-free-slot days from
    ``start_day`` onward, within the garage's own booking window - used to
    offer real alternatives when a requested day has nothing left."""
    now = now or datetime.now(UTC)
    settings = availability.resolve_settings(garage)
    _, window_end = availability.booking_window(settings, now.date())

    found: list[date] = []
    cursor = max(start_day, now.date())
    while cursor <= window_end and len(found) < limit:
        payload = get_availability_for_day(
            garage, cursor, appointment_type=appointment_type, now=now
        )
        if payload["is_open"] and any(
            s["status"] != availability.SLOT_BOOKED for s in payload["slots"]
        ):
            found.append(cursor)
        cursor += timedelta(days=1)
    return found


def find_customer(garage, phone_e164: str) -> Customer | None:
    """Identify an existing customer from a channel's own phone number -
    reuses the exact same lookup the WhatsApp inbound webhook uses
    (app/communications/service.py::find_customer_by_phone), so "does this
    number belong to a known customer" is answered identically everywhere."""
    return find_customer_by_phone(garage, phone_e164)


def find_vehicles_for_customer(customer: Customer) -> list[Vehicle]:
    return [v for v in customer.vehicles if v.is_active]


def find_customer_vehicle_by_registration(customer: Customer, registration: str) -> Vehicle | None:
    """A registration match *within this one customer's own vehicles only* -
    deliberately not a garage-wide search, so a registration that happens to
    belong to a different customer can never surface that other customer's
    vehicle details into this conversation."""
    normalized = registration.strip().upper().replace(" ", "")
    for vehicle in customer.vehicles:
        if vehicle.is_active and vehicle.registration_number == normalized:
            return vehicle
    return None


def get_upcoming_appointments(garage, customer: Customer, *, now=None) -> list[Appointment]:
    now = now or datetime.now(UTC)
    rows: list[Appointment] = (
        Appointment.query.filter(
            Appointment.garage_id == garage.id,
            Appointment.customer_id == customer.id,
            Appointment.status != "CANCELLED",
            Appointment.start_time >= now,
        )
        .order_by(Appointment.start_time.asc())
        .all()
    )
    return rows


def get_business_hours(garage) -> dict[int, tuple[time, time, bool]]:
    """``{weekday (0=Mon) -> (opens_at, closes_at, is_closed)}`` - the same
    opening-hours resolution the public calendar uses."""
    return availability.resolve_opening_hours(garage)


def get_mot_expiry(vehicle: Vehicle) -> date | None:
    """The vehicle's actual stored MOT expiry - never calculated/guessed."""
    return vehicle.mot_expiry_date


def revalidate_slot(
    garage, day: date, slot_time: time, *, appointment_type=None, now=None
) -> str | None:
    """``None`` if ``(day, slot_time)`` is still genuinely bookable right
    now, or a short reason code otherwise. Must be called again immediately
    before :func:`create_booking_request` actually creates anything - a slot
    offered several conversation turns ago may have been taken since
    (Part 9)."""
    now = now or datetime.now(UTC)
    return availability.validate_slot(
        garage, day, slot_time, now, appointment_type=appointment_type
    )


def create_booking_request(
    garage,
    *,
    customer: Customer | None = None,
    first_name: str,
    last_name: str,
    phone_e164: str,
    email: str | None,
    vehicle_registration: str,
    vehicle_make: str | None = None,
    vehicle_model: str | None = None,
    vehicle_year: int | None = None,
    vehicle_mileage: int | None = None,
    appointment_type: GarageAppointmentType | None,
    preferred_date: date,
    preferred_time: time | None,
    notes: str | None = None,
    now: datetime | None = None,
) -> tuple[BookingRequest | None, str | None]:
    """Create the exact same kind of PENDING booking request the public web
    form creates (app/public_booking/routes.py) - the conversation engine
    never invents a separate booking mechanism (Part 4).

    Re-validates the slot server-side one last time first (Part 8/9/37): if
    it's gone, nothing is created and ``(None, reason)`` is returned so the
    workflow can apologise and re-offer real alternatives, exactly as if two
    customers had raced for the same slot over the public booking page.
    """
    if preferred_time is not None:
        reason = revalidate_slot(
            garage,
            preferred_date,
            preferred_time,
            appointment_type=appointment_type,
            now=now,
        )
        if reason is not None:
            return None, reason

    booking_request = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference=unique_booking_reference(db.session),
        # Pre-linked when the customer is already known (e.g. identified by
        # phone - see app/communications/service.py::find_customer_by_phone).
        # Unlike the public web form (app/public_booking/routes.py), this
        # channel doesn't eagerly resolve-or-create a Customer/Vehicle for a
        # new caller - it never asks for an email, so there's nothing
        # reliable to match on later; that resolution still happens at
        # approval time (app/booking_requests/routes.py).
        customer_id=customer.id if customer else None,
        customer_first_name=first_name,
        customer_last_name=last_name,
        customer_email=email,
        customer_phone=phone_e164,
        vehicle_registration=vehicle_registration,
        vehicle_make=vehicle_make,
        vehicle_model=vehicle_model,
        vehicle_year=vehicle_year,
        vehicle_mileage=vehicle_mileage,
        appointment_type_id=appointment_type.id if appointment_type else None,
        requested_duration_minutes=(
            appointment_type.default_duration_minutes if appointment_type else None
        ),
        requested_price=appointment_type.base_price if appointment_type else None,
        preferred_date=preferred_date,
        preferred_time=preferred_time,
        notes=notes,
    )
    db.session.add(booking_request)
    db.session.commit()

    emit_event(BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request)
    return booking_request, None


def cancel_appointment(garage, appointment: Appointment) -> tuple[bool, str | None]:
    """Cancels in place (status -> CANCELLED) - never deletes, so history
    and every existing audit hook (checklists, communications) keeps
    working (Part 14)."""
    if appointment.status == "CANCELLED":
        return False, "already_cancelled"
    if appointment.status == "COMPLETED":
        return False, "already_completed"

    appointment.status = "CANCELLED"
    db.session.commit()

    emit_event(APPOINTMENT_CANCELLED, garage=garage, appointment=appointment)
    return True, None


def reschedule_appointment(
    garage, appointment: Appointment, new_day: date, new_time: time
) -> tuple[bool, str | None]:
    """Moves an appointment to a new day/time, keeping its existing
    appointment type and assigned employee and its original duration
    (Part 15). Re-validates real availability for the new slot and the
    assigned employee's own conflict-free-ness before committing anything -
    the same two checks the staff PATCH endpoint enforces
    (app/appointments/routes.py), applied here so a conversational
    reschedule can never double-book either the garage or one employee.
    """
    duration = appointment.end_time - appointment.start_time
    new_start = datetime.combine(new_day, new_time, tzinfo=UTC)
    new_end = new_start + duration

    reason = revalidate_slot(
        garage, new_day, new_time, appointment_type=appointment.appointment_type
    )
    if reason is not None:
        return False, reason

    conflict = Appointment.query.filter(
        Appointment.employee_id == appointment.employee_id,
        Appointment.id != appointment.id,
        Appointment.status != "CANCELLED",
        Appointment.start_time < new_end,
        Appointment.end_time > new_start,
    ).first()
    if conflict is not None:
        return False, "employee_conflict"

    appointment.start_time = new_start
    appointment.end_time = new_end
    db.session.commit()

    emit_event(APPOINTMENT_RESCHEDULED, garage=garage, appointment=appointment)
    return True, None


def update_customer_name(
    garage, customer: Customer, first_name: str, last_name: str
) -> tuple[bool, str | None]:
    """Correct the customer's own name on the canonical ``Customer`` row -
    the same record the staff UI, calls, WhatsApp inbox and appointments all
    read from, so the fix is global, not session-local. Tenant-scoped: only
    ever touches a customer that belongs to ``garage``. Name only - never
    email, phone, or any business-/security-controlled field."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last or len(first) > 100 or len(last) > 100:
        return False, "invalid_name"
    if customer.garage_id != garage.id:
        return False, "cross_tenant"
    customer.first_name = first
    customer.last_name = last
    db.session.commit()
    return True, None


def create_callback_request(
    garage,
    *,
    customer: Customer | None,
    phone_e164: str,
    reason: str | None = None,
    preferred_time: str | None = None,
    session=None,
) -> CallbackRequest:
    callback = CallbackRequest(
        garage_id=garage.id,
        customer_id=customer.id if customer else None,
        phone_number=phone_e164,
        reason=reason,
        preferred_time=preferred_time,
        source_session_id=session.id if session is not None else None,
    )
    db.session.add(callback)
    db.session.commit()

    emit_event(CALLBACK_REQUESTED, garage=garage, callback_request=callback)
    return callback
