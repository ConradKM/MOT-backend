"""Grouped counts and rates that more than one statistics surface reports.

Platform Admin's per-tenant page (app/platform_admin/stats.py) and a
business's own Insights page (app/insights) both report appointment outcomes
and booking-request decisions. They must agree on what a "no-show rate" or an
"approval rate" *is*, so both derive them here. What they may legitimately
differ on is which rows fall in a period, so that is always an explicit
parameter rather than a hidden assumption:

* ``basis`` names the timestamp a row is bucketed by. An appointment "this
  month" to a business means one whose ``start_time`` is in the month;
  Platform Admin's usage figures count rows by ``created_at``.
* ``start``/``end`` are aware UTC instants, half-open (``start <= t < end``).
  ``end=None`` means "up to now and beyond", which is what Platform Admin's
  rolling windows have always meant.

The rules from app/platform_admin/stats.py apply: aggregate in SQL, and a
rate over an empty denominator is ``None``, never ``0``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.booking_request import BookingRequest

DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 730

#: Which appointment timestamp places a row in a period.
AppointmentBasis = Literal["start_time", "created_at"]


def clamp_days(days: int | None) -> int:
    if not days:
        return DEFAULT_PERIOD_DAYS
    return max(1, min(int(days), MAX_PERIOD_DAYS))


def rate(numerator: int, denominator: int) -> float | None:
    """A percentage, or ``None`` when there is nothing to divide by."""
    if not denominator:
        return None
    return round(100 * numerator / denominator, 1)


def grouped(column, *filters) -> dict:
    """``{column value: count}`` for one grouped query."""
    query = select(column, func.count())
    if filters:
        query = query.where(*filters)
    rows = db.session.execute(query.group_by(column)).all()
    return {key: count for key, count in rows}


def _window(column, start: datetime, end: datetime | None) -> list:
    filters = [column >= start]
    if end is not None:
        filters.append(column < end)
    return filters


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------


def appointment_filters(
    garage_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime | None = None,
    basis: AppointmentBasis = "start_time",
    employee_id: uuid.UUID | None = None,
) -> list:
    """The WHERE clause for "this business's appointments in this period"."""
    filters = [Appointment.garage_id == garage_id]
    filters += _window(getattr(Appointment, basis), start, end)
    if employee_id is not None:
        filters.append(Appointment.employee_id == employee_id)
    return filters


def appointment_status_counts(
    garage_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime | None = None,
    basis: AppointmentBasis = "start_time",
    employee_id: uuid.UUID | None = None,
) -> dict[str, int]:
    return grouped(
        Appointment.status,
        *appointment_filters(garage_id, start=start, end=end, basis=basis, employee_id=employee_id),
    )


def summarise_appointment_statuses(by_status: dict[str, int]) -> dict:
    total = sum(by_status.values())
    completed = by_status.get("COMPLETED", 0)
    no_show = by_status.get("NO_SHOW", 0)
    cancelled = by_status.get("CANCELLED", 0)
    # Appointments whose outcome is known. An upcoming BOOKED slot isn't a
    # missed one, so it must not drag the no-show rate down.
    concluded = completed + no_show + cancelled

    return {
        "total": total,
        "by_status": by_status,
        "completed": completed,
        "no_show": no_show,
        "cancelled": cancelled,
        "upcoming": by_status.get("BOOKED", 0) + by_status.get("REQUESTED", 0),
        "completion_rate": rate(completed, concluded),
        "no_show_rate": rate(no_show, completed + no_show),
        "cancellation_rate": rate(cancelled, concluded),
    }


# --------------------------------------------------------------------------
# Booking requests
# --------------------------------------------------------------------------


def booking_request_status_counts(
    garage_id: uuid.UUID, *, start: datetime, end: datetime | None = None
) -> dict[str, int]:
    """Requests *received* in the period - always bucketed by ``created_at``,
    since a request's preferred date says nothing about when it arrived."""
    return grouped(
        BookingRequest.status,
        BookingRequest.garage_id == garage_id,
        *_window(BookingRequest.created_at, start, end),
    )


def summarise_booking_requests(by_status: dict[str, int]) -> dict:
    approved = by_status.get("APPROVED", 0)
    rejected = by_status.get("REJECTED", 0)
    expired = by_status.get("EXPIRED", 0)
    pending = by_status.get("PENDING", 0)
    reviewed = approved + rejected
    total = sum(by_status.values())

    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "expired": expired,
        # Of the requests staff actually made a decision on. Expired requests
        # are excluded - nobody decided them, so counting them as rejections
        # would blame staff for a queue they never saw.
        "approval_rate": rate(approved, reviewed),
        "rejection_rate": rate(rejected, reviewed),
        # Of everything received: how much was left to time out.
        "expiry_rate": rate(expired, total),
    }
