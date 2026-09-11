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
from datetime import UTC, datetime

from flask_smorest import abort
from sqlalchemy import and_, or_

from app.extensions import db
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
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
    vehicle_registration: str,
    vehicle_make: str | None,
    vehicle_model: str | None,
    vehicle_year: int | None,
    vehicle_mileage: int | None,
) -> tuple[Customer, Vehicle]:
    """Reuse-or-create the ``Customer`` + ``Vehicle`` a booking (request)
    refers to. Shared by the public web form - which resolves this eagerly,
    at submission time (see app/public_booking/routes.py) - and staff
    approval (app/booking_requests/routes.py), which mostly just re-finds
    what the public form already created; it's still needed as-is for
    conversation-engine requests, which don't resolve eagerly.
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
        abort(
            409,
            message="A vehicle with this registration already exists for a different "
            "customer - resolve it manually before approving.",
        )
    elif not vehicle.is_active:
        vehicle.is_active = True

    return customer, vehicle


def is_request_stale(booking_request: BookingRequest, now: datetime | None = None) -> bool:
    """True once the request's preferred date/time has passed.

    A date-only request (no preferred_time) is treated as stale once its
    whole preferred day has passed - there is no time-of-day to compare, and
    the day itself is still "current" until it ends.
    """
    now = now or datetime.now(UTC)
    today = now.date()

    if booking_request.preferred_date < today:
        return True
    if booking_request.preferred_date > today:
        return False
    # Same day as today.
    if booking_request.preferred_time is None:
        return False
    return booking_request.preferred_time < now.time()


def expire_stale_booking_requests(garage_id=None, now: datetime | None = None, session=None) -> int:
    """Flip every stale PENDING request to EXPIRED (releasing whatever
    capacity it was holding). Returns how many were changed.

    Cheap and safe to call on every staff-facing read of booking requests -
    it only ever touches rows that are already stale, and is a no-op when
    there aren't any.
    """
    session = session or db.session
    now = now or datetime.now(UTC)
    today = now.date()

    query = BookingRequest.query.filter(BookingRequest.status == "PENDING")
    if garage_id is not None:
        query = query.filter(BookingRequest.garage_id == garage_id)

    timed_and_passed = and_(
        BookingRequest.preferred_time.isnot(None),
        or_(
            BookingRequest.preferred_date < today,
            and_(
                BookingRequest.preferred_date == today,
                BookingRequest.preferred_time < now.time(),
            ),
        ),
    )
    date_only_and_passed = and_(
        BookingRequest.preferred_time.is_(None),
        BookingRequest.preferred_date < today,
    )

    stale_ids = [row.id for row in query.filter(or_(timed_and_passed, date_only_and_passed)).all()]
    if not stale_ids:
        return 0

    BookingRequest.query.filter(BookingRequest.id.in_(stale_ids)).update(
        {"status": "EXPIRED"}, synchronize_session=False
    )
    session.commit()
    return len(stale_ids)


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
    slot_start = datetime.combine(
        booking_request.preferred_date, booking_request.preferred_time, tzinfo=UTC
    )
    garage = booking_request.garage
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
    """Best-known duration: the type's current default when it's still
    around, else the snapshot taken at submission time (covers a type edited
    or - since it's a nullable FK - deleted while this request was pending),
    else the garage's generic default."""
    if (
        booking_request.appointment_type
        and booking_request.appointment_type.default_duration_minutes
    ):
        return booking_request.appointment_type.default_duration_minutes
    if booking_request.requested_duration_minutes is not None:
        return booking_request.requested_duration_minutes
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
        # Only ever meaningful for the request a reject just acted on - the
        # reject route sets this *before* calling attach_review_context, so
        # default it here for every other read path (list, get, approve) so
        # the schema's getattr never raises on a plain instance.
        if not hasattr(r, "_notification_result"):
            r._notification_result = None
