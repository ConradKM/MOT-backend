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

from datetime import UTC, datetime

from sqlalchemy import and_, or_

from app.extensions import db
from app.models.booking_request import BookingRequest
from app.public_booking.availability import resolve_settings, slot_capacity_usage


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


def expire_stale_booking_requests(
    garage_id=None, now: datetime | None = None, session=None
) -> int:
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

    stale_ids = [
        row.id
        for row in query.filter(or_(timed_and_passed, date_only_and_passed)).all()
    ]
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
    if booking_request.appointment_type and booking_request.appointment_type.default_duration_minutes:
        return booking_request.appointment_type.default_duration_minutes
    if booking_request.requested_duration_minutes is not None:
        return booking_request.requested_duration_minutes
    if booking_request.garage is None:
        return None
    return resolve_settings(booking_request.garage).default_appointment_minutes


def attach_review_context(
    requests: list[BookingRequest], now: datetime | None = None
) -> None:
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
