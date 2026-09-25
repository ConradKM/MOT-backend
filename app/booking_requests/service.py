"""Booking-request lifecycle helpers: expiry sweeping and the slot-conflict
check shown on the staff review page.

A PENDING request reserves capacity for its preferred slot for as long as it
stays PENDING (see app/public_booking/availability.py - pending requests are
counted the same as real appointments there). Once a request leaves PENDING -
APPROVED, REJECTED, or EXPIRED - that reservation is released automatically,
simply because the availability queries only ever look at
``status == "PENDING"``. This module's only job is making sure a request whose
preferred time has passed doesn't stay PENDING (and therefore reserved and
"actionable") forever.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from flask import current_app, g
from flask_smorest import abort

from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.communications.events import BOOKING_REQUEST_APPROVED, emit_event
from app.extensions import db
from app.garages.timezones import local_day_for, local_slot_as_utc, timezone_for
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.vehicle import Vehicle
from app.public_booking.availability import resolve_settings, slot_capacity_usage


def _normalize_registration(value: str) -> str:
    # Mirrors app/models/vehicle.py::Vehicle.normalize_registration_number so
    # a lookup matches however the reg was originally stored.
    return value.strip().upper().replace(" ", "")


def resolve_customer_and_vehicle(
    garage_id: uuid.UUID,
    *,
    customer_id: uuid.UUID | None,
    customer_email: str | None,
    first_name: str,
    last_name: str,
    phone: str | None,
    vehicle_registration: str | None,
    vehicle_make: str | None = None,
    vehicle_model: str | None = None,
    vehicle_year: int | None = None,
    vehicle_mileage: int | None = None,
) -> tuple[Customer, Vehicle | None]:
    """Reuse-or-create the ``Customer`` - and the tracked item, when there is
    one - that a booking (request) refers to.

    Shared by the public web form, which resolves this eagerly at submission
    time (see app/public_booking/routes.py), and staff approval
    (app/booking_requests/routes.py), which mostly just re-finds what the
    public form already created; it is still needed as-is for
    conversation-engine requests, which don't resolve eagerly.

    ``vehicle_registration`` is now optional, and None is a perfectly ordinary
    outcome rather than an error: since the booking form became
    business-configurable, the identifier of whatever is being booked in is an
    ordinary field with a binding, and a business that tracks no item at all -
    a salon, a clinic - never collects one. The customer is always resolved;
    the item is returned as None when nothing identified it.
    """
    customer = None
    if customer_id is not None:
        customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
    if customer is None and customer_email:
        customer = Customer.query.filter(
            Customer.garage_id == garage_id,
            Customer.email.ilike(customer_email),
        ).first()
    if customer is None:
        customer = Customer(
            garage_id=garage_id,
            first_name=first_name,
            last_name=last_name,
            email=customer_email,
            phone=phone,
        )
        db.session.add(customer)
        db.session.flush()
    elif not customer.is_active:
        # An archived customer matched by email - bring them back rather than
        # silently creating a duplicate or letting the booking attach to a
        # customer nobody can see in the normal list.
        customer.is_active = True

    if not vehicle_registration or not vehicle_registration.strip():
        # No identifier collected - the business tracks no item. Both
        # BookingRequest.vehicle_id and Appointment.vehicle_id are nullable,
        # so the booking is complete without one.
        return customer, None

    reg = _normalize_registration(vehicle_registration)
    vehicle = Vehicle.query.filter_by(garage_id=garage_id, registration_number=reg).first()
    if vehicle is None:
        vehicle = Vehicle(
            garage_id=garage_id,
            customer_id=customer.id,
            registration_number=vehicle_registration,
            make=vehicle_make,
            model=vehicle_model,
            year=vehicle_year,
            current_mileage=vehicle_mileage,
        )
        db.session.add(vehicle)
        db.session.flush()
    elif vehicle.customer_id != customer.id:
        # A second, previously-unlogged 409 source in the public deposit-intent
        # call chain (_build_booking_request -> here), entirely unrelated to
        # slot/time availability - yet MOT-frontend's DepositStep used to map
        # every 409 on that endpoint to "This time is no longer available",
        # and this one never went through app/public_booking/routes.py's
        # AVAILABILITY_REJECTED logging, so it was invisible to that
        # diagnostic. Reproduced live: two different customers (emails) each
        # submitting the same normalised registration at the same garage - one
        # of the more mundane ways to hit this on a shared demo/test business,
        # since a placeholder-looking plate ("AB12 CDE") is exactly the kind
        # of value more than one tester types. No PII here - the registration
        # itself, names and emails stay out of the log; only ids.
        current_app.logger.warning(
            "VEHICLE_REFERENCE_CONFLICT request_id=%s garage=%s existing_customer=%s "
            "new_customer=%s vehicle=%s",
            g.get("request_id"),
            garage_id,
            vehicle.customer_id,
            customer.id,
            vehicle.id,
        )
        abort(
            409,
            message="An item with this reference already exists for a different "
            "customer - resolve it manually before approving.",
            errors={"reason": "vehicle_reference_conflict"},
        )
    elif not vehicle.is_active:
        vehicle.is_active = True

    return customer, vehicle


def approve_booking_request(
    *, reviewer: Employee, request_id: uuid.UUID, data: Mapping[str, Any]
) -> BookingRequest:
    """Authoritatively approve one pending request into one appointment.

    This is the sole manual-approval transaction. It owns the garage/request
    locks and all revalidation before an appointment is made, so a future
    automatic path can reuse this operation rather than inventing another
    booking-to-appointment flow. Callers must provide an explicit, valid
    employee assignment; this function intentionally does not select one.
    """
    garage_id = reviewer.garage_id
    # Use the same per-garage lock as public booking submission. Without it,
    # different pending requests can both pass capacity before either
    # appointment is committed.
    db.session.query(Garage).filter_by(id=garage_id).with_for_update().one()
    booking_request = cast(
        BookingRequest | None,
        BookingRequest.query.filter_by(id=request_id, garage_id=garage_id)
        .with_for_update()
        .first(),
    )
    if booking_request is None:
        abort(404, message="Booking request not found")
    # flask-smorest's abort raises at runtime, but its type annotation does
    # not express that control flow to mypy.
    assert booking_request is not None

    if booking_request.status == "PENDING" and is_request_stale(booking_request):
        booking_request.status = "EXPIRED"
        db.session.commit()
        abort(
            409,
            message="This request's preferred time has already passed - it has expired and can no longer be approved.",
        )
    if booking_request.status != "PENDING":
        abort(
            409, message=f"This booking request has already been {booking_request.status.lower()}."
        )

    appointment_type_id = data.get("appointment_type_id") or booking_request.appointment_type_id
    if appointment_type_id is None:
        abort(422, message="appointment_type_id is required to create the appointment.")
    appointment_type = GarageAppointmentType.query.filter_by(
        id=appointment_type_id, garage_id=garage_id
    ).first()
    if appointment_type is None or appointment_type.status != "ACTIVE":
        abort(422, message="appointment_type_id is not an active type for this business.")

    assigned_employee_id = data.get("employee_id")
    if assigned_employee_id is None:
        abort(422, message="employee_id is required to schedule the appointment.")
    assigned_employee = Employee.query.filter_by(
        id=assigned_employee_id, garage_id=garage_id
    ).first()
    if assigned_employee is None:
        abort(422, message="employee_id does not belong to your business.")
    if not assigned_employee.is_active:
        abort(422, message="This employee's account is deactivated.")

    start_time, end_time = _resolve_appointment_slot(booking_request, data, appointment_type)
    _assert_no_conflict(assigned_employee_id, start_time, end_time)
    _assert_capacity_available(booking_request.garage, booking_request, start_time, end_time)

    customer, vehicle = resolve_customer_and_vehicle(
        garage_id,
        customer_id=booking_request.customer_id,
        customer_email=booking_request.customer_email,
        first_name=booking_request.customer_first_name,
        last_name=booking_request.customer_last_name,
        phone=booking_request.customer_phone,
        vehicle_registration=booking_request.vehicle_registration,
        vehicle_make=booking_request.vehicle_make,
        vehicle_model=booking_request.vehicle_model,
        vehicle_year=booking_request.vehicle_year,
        vehicle_mileage=booking_request.vehicle_mileage,
    )
    appointment = Appointment(
        garage_id=garage_id,
        employee_id=assigned_employee_id,
        customer_id=customer.id,
        vehicle_id=None if vehicle is None else vehicle.id,
        appointment_type_id=appointment_type.id,
        start_time=start_time,
        end_time=end_time,
        status="BOOKED",
        notes=booking_request.notes,
        price_at_booking=(
            booking_request.requested_price
            if booking_request.requested_price is not None
            else appointment_type.base_price
        ),
        appointment_type_name_at_booking=(
            booking_request.requested_appointment_type_name or appointment_type.name
        ),
    )
    db.session.add(appointment)
    db.session.flush()
    snapshot_checklist_for_appointment(appointment)

    booking_request.status = "APPROVED"
    booking_request.appointment_type_id = appointment_type.id
    booking_request.customer_id = customer.id
    booking_request.vehicle_id = None if vehicle is None else vehicle.id
    booking_request.appointment_id = appointment.id
    booking_request.reviewed_by_employee_id = reviewer.id
    booking_request.reviewed_at = datetime.now(UTC)
    booking_request.accepted_automatically = bool(data.get("accepted_automatically", False))
    if data.get("staff_notes") is not None:
        booking_request.staff_notes = data["staff_notes"]

    db.session.commit()
    emit_event(
        BOOKING_REQUEST_APPROVED,
        garage=booking_request.garage,
        booking_request=booking_request,
        appointment=appointment,
    )
    return booking_request


def auto_accept_booking_request(*, garage_id: uuid.UUID, request_id: uuid.UUID) -> BookingRequest | None:
    """Accept a new request only when the configured tenant can safely do so.

    A stable active-employee ordering is the assignment policy: the first
    employee without an overlapping appointment is selected.  The actual
    transition is still delegated to :func:`approve_booking_request`, which
    takes the same garage/request locks and repeats every authoritative check.
    A request that cannot be assigned remains PENDING for staff review.
    """
    garage = db.session.get(Garage, garage_id)
    if (
        garage is None
        or not garage.auto_accept_booking_requests
        or not garage.auto_accept_booking_requests_enabled
    ):
        return None

    request = BookingRequest.query.filter_by(id=request_id, garage_id=garage_id).first()
    if request is None or request.status != "PENDING" or request.preferred_time is None:
        return None
    appointment_type = request.appointment_type
    if appointment_type is None or appointment_type.status != "ACTIVE":
        return None

    duration_minutes = request.requested_duration_minutes or appointment_type.default_duration_minutes
    if duration_minutes is None:
        # A legacy request without a duration can still be reviewed manually.
        return None
    start_time = local_slot_as_utc(garage, request.preferred_date, request.preferred_time)
    end_time = start_time + timedelta(minutes=duration_minutes)

    # A capacity check is only a fast fail before assignment selection.  The
    # approval service repeats it under the garage lock before writing.
    duration_min = int((end_time - start_time).total_seconds() // 60)
    used, capacity = slot_capacity_usage(
        garage, start_time.date(), start_time, duration_min, exclude_request_id=request.id
    )
    if used >= capacity:
        return None

    candidates = Employee.query.filter_by(garage_id=garage_id, is_active=True).order_by(Employee.id).all()
    for employee in candidates:
        clash = Appointment.query.filter(
            Appointment.employee_id == employee.id,
            Appointment.status != "CANCELLED",
            Appointment.start_time < end_time,
            Appointment.end_time > start_time,
        ).first()
        if clash is not None:
            continue
        approved = approve_booking_request(
            reviewer=employee,
            request_id=request.id,
            data={"employee_id": employee.id, "accepted_automatically": True},
        )
        return approved
    return None


def _resolve_appointment_slot(booking_request, data, appointment_type):
    start_time = data.get("start_time")
    if start_time is None and booking_request.preferred_time is not None:
        start_time = local_slot_as_utc(
            booking_request.garage, booking_request.preferred_date, booking_request.preferred_time
        )
    if start_time is None:
        abort(
            422,
            message="start_time is required - the request has no preferred time to fall back on.",
        )
    end_time = data.get("end_time")
    if end_time is None:
        duration_minutes = (
            booking_request.requested_duration_minutes
            if booking_request.requested_duration_minutes is not None
            else appointment_type.default_duration_minutes
        )
        if duration_minutes is None:
            abort(
                422, message="end_time is required - this appointment type has no default duration."
            )
        end_time = start_time + timedelta(minutes=duration_minutes)
    if start_time >= end_time:
        abort(422, message="start_time must be before end_time.")
    return start_time, end_time


def _assert_no_conflict(employee_id, start_time, end_time):
    clash = Appointment.query.filter(
        Appointment.employee_id == employee_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
    ).first()
    if clash is not None:
        abort(409, message="The selected employee already has an appointment during this time.")


def _assert_capacity_available(garage, booking_request, start_time, end_time):
    duration_min = int((end_time - start_time).total_seconds() // 60)
    used, capacity = slot_capacity_usage(
        garage,
        start_time.date(),
        start_time,
        duration_min,
        exclude_request_id=booking_request.id,
    )
    if used >= capacity:
        abort(
            409,
            message="This time is no longer available - capacity has already been taken by another appointment or request.",
        )


def is_request_stale(booking_request: BookingRequest, now: datetime | None = None) -> bool:
    """True once the request's preferred date/time has passed.

    A date-only request (no preferred_time) is treated as stale once its
    whole preferred day has passed - there is no time-of-day to compare, and
    the day itself is still "current" until it ends.
    """
    now = now or datetime.now(UTC)
    today = local_day_for(booking_request.garage, now)

    if booking_request.preferred_date < today:
        return True
    if booking_request.preferred_date > today:
        return False
    # Same day as today.
    if booking_request.preferred_time is None:
        return False
    return (
        booking_request.preferred_time < now.astimezone(timezone_for(booking_request.garage)).time()
    )


def expire_stale_booking_requests(garage_id=None, now: datetime | None = None, session=None) -> int:
    """Flip every stale PENDING request to EXPIRED (releasing whatever
    capacity it was holding). Returns how many were changed.

    Cheap and safe to call on every staff-facing read of booking requests -
    it only ever touches rows that are already stale, and is a no-op when
    there aren't any.
    """
    session = session or db.session
    now = now or datetime.now(UTC)
    query = BookingRequest.query.filter(BookingRequest.status == "PENDING")
    if garage_id is not None:
        query = query.filter(BookingRequest.garage_id == garage_id)
    # ``preferred_date`` / ``preferred_time`` are wall-clock values, so a
    # single SQL comparison against UTC's calendar date is wrong for a
    # multi-timezone tenant set. Evaluate the small pending set through the
    # same business-local predicate staff approval uses.
    stale_ids = [row.id for row in query.all() if is_request_stale(row, now)]
    if not stale_ids:
        return 0

    # The initial read is deliberately cheap, but it must not be treated as
    # authority for the later write: a reviewer can approve one of these
    # requests in the gap.  Keep the state precondition in the UPDATE itself
    # so expiry can never overwrite a completed approval.
    expired = (
        session.query(BookingRequest)
        .filter(
            BookingRequest.id.in_(stale_ids),
            BookingRequest.status == "PENDING",
        )
        .update({"status": "EXPIRED"}, synchronize_session=False)
    )
    session.commit()
    return int(expired)


def slot_check_for_request(booking_request: BookingRequest, now: datetime | None = None):
    """``{"checked": bool, "available": bool|None, "reason": str|None}`` - the
    "current availability/conflict status" shown on the request review screen.

    Only meaningful for a still-PENDING request with a specific preferred
    time; anything else (no preferred time, already decided/expired) reports
    ``checked: False`` rather than a misleading always-true/false guess.
    """
    if booking_request.status != "PENDING" or booking_request.preferred_time is None:
        return {"checked": False, "available": None, "reason": None}

    now = now or datetime.now(UTC)
    garage = booking_request.garage
    slot_start = local_slot_as_utc(
        garage, booking_request.preferred_date, booking_request.preferred_time
    )
    # The request's *own* selected duration, not the garage's flat default -
    # otherwise a 90-minute Full Service request could be re-checked as if it
    # only needed the garage's generic slot length, understating what it
    # actually still needs to fit.
    duration = _duration_minutes_for(booking_request)
    # booking_request.garage_id is a non-nullable FK, so the garage-is-None
    # branch in _duration_minutes_for is unreachable here.
    assert duration is not None
    used, capacity = slot_capacity_usage(
        garage,
        booking_request.preferred_date,
        slot_start,
        duration,
        exclude_request_id=booking_request.id,
    )
    # `used` excludes this request's own reservation, so "available" means
    # there is still room for it specifically (used < capacity), not that the
    # slot is entirely empty.
    return {"checked": True, "available": used < capacity, "reason": None}


def _duration_minutes_for(booking_request: BookingRequest) -> int | None:
    """Best-known duration, preferring the immutable request snapshot.

    This must mirror public availability: an owner changing a service's
    duration after the customer submitted cannot rewrite the reservation or
    the staff review's capacity check for that request.
    """
    if booking_request.requested_duration_minutes is not None:
        return booking_request.requested_duration_minutes
    if (
        booking_request.appointment_type
        and booking_request.appointment_type.default_duration_minutes
    ):
        return booking_request.appointment_type.default_duration_minutes
    if booking_request.garage is None:
        return None
    return resolve_settings(booking_request.garage).default_appointment_minutes


def attach_review_context(requests: list[BookingRequest], now: datetime | None = None) -> None:
    """Populate the transient attributes ``BookingRequestSchema`` reads for
    the staff review screen - one place, used by list/detail/approve/reject,
    so the enrichment logic isn't duplicated across routes."""
    now = now or datetime.now(UTC)

    reviewer_ids = {r.reviewed_by_employee_id for r in requests if r.reviewed_by_employee_id}
    names: dict = {}
    if reviewer_ids:
        from app.models.employee import Employee

        for emp in Employee.query.filter(Employee.id.in_(reviewer_ids)).all():
            full = " ".join(p for p in (emp.first_name, emp.last_name) if p)
            names[emp.id] = full or emp.email

    for r in requests:
        r._duration_minutes = _duration_minutes_for(r)
        r._reviewed_by_name = names.get(r.reviewed_by_employee_id)
        r._slot_check = slot_check_for_request(r, now)
        # Only ever meaningful on the response to a reject that just
        # happened - reset unconditionally on every call, not just when
        # unset. SQLAlchemy's identity map means the *same* Python object can
        # be handed back across requests within one session (as it is in the
        # test suite, and can be within one worker process), so a stale
        # `_notification_result` set by an earlier reject would otherwise
        # leak into a later, unrelated GET/list read of the same request. The
        # reject route sets the real value on the object *after* calling this
        # (see routes.py), overwriting the None set here.
        r._notification_result = None
