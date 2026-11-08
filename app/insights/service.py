"""Everything the Insights page shows, computed from live rows.

The rules from app/platform_admin/stats.py hold here too: every figure is a
grouped SQL query (no Python loops over fetched rows), and a rate over an
empty denominator is ``None``, never ``0``.

Two things are decided by :class:`Scope`, never by the caller's choice of
what to render:

* **Whose appointments.** Non-owner staff are always scoped to their own
  (``Appointment.employee_id``); an owner may narrow to one employee.
* **Whether money appears.** Only owners see money. For everyone else the
  money keys are never computed, so they cannot leak through serialisation -
  they are absent from the payload, not zeroed.

Money is integer pence while it is being added up and GBP ``Decimal`` in the
returned payload. Three figures are kept apart on purpose:

* **booked value** - the snapshotted price (add-ons and discount included) of
  COMPLETED appointments starting in the period. Work done, not money banked.
* **deposits collected** - online deposits charged in the period, net of
  refunds. Money actually taken.
* **lost value** - the price of NO_SHOW and CANCELLED appointments. Reported
  on its own, never netted out of either figure above.

Appointments without a price still count everywhere bookings are counted;
they are just absent from the money, and ``unpriced_appointments`` says how
many there were.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Integer, case, cast, exists, extract, func, select

from app.analytics.queries import (
    appointment_filters,
    appointment_status_counts,
    booking_request_status_counts,
    rate,
    summarise_appointment_statuses,
    summarise_booking_requests,
)
from app.extensions import db
from app.garages.timezones import timezone_for
from app.models.appointments.applied_add_on import AppointmentAddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.payments.payment import PAYMENT_STATUSES_CHARGED, BookingPayment
from app.models.queueing.queue_entry import QueueEntry

from .periods import Period

COMPLETED = "COMPLETED"
LOST_STATUSES = ("NO_SHOW", "CANCELLED")
TOP_LIMIT = 20

LOCKED = {"locked": True, "feature": "insights_advanced"}


@dataclass(frozen=True)
class Scope:
    garage: Garage
    #: Narrow to this employee's appointments; ``None`` is the whole business.
    employee_id: uuid.UUID | None
    #: Owners only. False means money keys are never computed.
    money: bool
    #: The ``insights_advanced`` feature is on for this business.
    advanced: bool

    @property
    def garage_id(self) -> uuid.UUID:
        return self.garage.id

    @property
    def tz_name(self) -> str:
        return timezone_for(self.garage).key

    def appointments(self, period: Period) -> list:
        return appointment_filters(
            self.garage_id,
            start=period.start,
            end=period.end,
            basis="start_time",
            employee_id=self.employee_id,
        )


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------


def to_pence(value) -> int:
    """A Numeric(10, 2) GBP sum -> integer pence."""
    if value is None:
        return 0
    return int((Decimal(value) * 100).quantize(Decimal(1)))


def to_gbp(pence: int) -> Decimal:
    return (Decimal(pence) / 100).quantize(Decimal("0.01"))


def _sum_price_where(*conditions):
    return func.sum(Appointment.price_at_booking).filter(*conditions)


def _local(column, scope: Scope):
    """A timestamptz as the business's local wall-clock timestamp."""
    return func.timezone(scope.tz_name, column)


# --------------------------------------------------------------------------
# Summary (basic)
# --------------------------------------------------------------------------


def _first_completed_visit():
    """Each customer's first COMPLETED appointment with the business, ever.
    Business-wide even when scoped to one employee: "new" means new to the
    business, not new to that member of staff."""
    return (
        select(
            Appointment.customer_id.label("customer_id"),
            func.min(Appointment.start_time).label("first_at"),
        )
        .where(Appointment.status == COMPLETED)
        .group_by(Appointment.customer_id)
    )


def _customers_completed_in(scope: Scope, period: Period):
    return (
        select(Appointment.customer_id.label("customer_id"))
        .where(*scope.appointments(period), Appointment.status == COMPLETED)
        .distinct()
        .subquery()
    )


def _customer_split(scope: Scope, period: Period) -> dict:
    """New vs returning, among customers with a completed visit in the period.

    Returning: also had a completed visit before the period started. New:
    their first completed visit falls in the period - including a customer
    whose first *and* second visits are both in it.
    """
    visited = _customers_completed_in(scope, period)
    first = _first_completed_visit().where(Appointment.garage_id == scope.garage_id).subquery()
    new, returning = db.session.execute(
        select(
            func.count().filter(first.c.first_at >= period.start),
            func.count().filter(first.c.first_at < period.start),
        )
        .select_from(visited)
        .join(first, first.c.customer_id == visited.c.customer_id)
    ).one()
    new, returning = int(new or 0), int(returning or 0)
    return {
        "customers_seen": new + returning,
        "new_customers": new,
        "returning_customers": returning,
        "returning_rate": rate(returning, new + returning),
    }


def _money(scope: Scope, period: Period) -> dict:
    booked, discounts, lost, unpriced = db.session.execute(
        select(
            _sum_price_where(Appointment.status == COMPLETED),
            func.sum(Appointment.discount_amount).filter(Appointment.status == COMPLETED),
            _sum_price_where(Appointment.status.in_(LOST_STATUSES)),
            func.count().filter(
                Appointment.status.in_((COMPLETED, *LOST_STATUSES)),
                Appointment.price_at_booking.is_(None),
            ),
        ).where(*scope.appointments(period))
    ).one()

    net = BookingPayment.amount_minor - func.coalesce(BookingPayment.refunded_amount_minor, 0)
    paid_at = func.coalesce(BookingPayment.paid_at, BookingPayment.created_at)
    deposits_query = select(func.coalesce(func.sum(net), 0), func.count()).where(
        BookingPayment.garage_id == scope.garage_id,
        BookingPayment.status.in_(PAYMENT_STATUSES_CHARGED),
        paid_at >= period.start,
        paid_at < period.end,
    )
    if scope.employee_id is not None:
        deposits_query = deposits_query.join(
            Appointment, Appointment.id == BookingPayment.appointment_id
        ).where(Appointment.employee_id == scope.employee_id)
    deposits_pence, deposits_count = db.session.execute(deposits_query).one()

    return {
        "booked_value": to_gbp(to_pence(booked)),
        "discounts_given": to_gbp(to_pence(discounts)),
        "lost_value": to_gbp(to_pence(lost)),
        "deposits_collected": to_gbp(int(deposits_pence or 0)),
        "deposits_count": int(deposits_count or 0),
        "unpriced_appointments": int(unpriced or 0),
    }


def summary(scope: Scope, period: Period) -> dict:
    appointments = summarise_appointment_statuses(
        appointment_status_counts(
            scope.garage_id,
            start=period.start,
            end=period.end,
            basis="start_time",
            employee_id=scope.employee_id,
        )
    )
    result: dict = {
        "appointments": appointments,
        "lost_count": appointments["no_show"] + appointments["cancelled"],
        **_customer_split(scope, period),
        # Requests aren't assigned to a member of staff, so there is no honest
        # per-employee figure - the section is left out of a scoped view.
        "booking_requests": (
            None
            if scope.employee_id is not None
            else summarise_booking_requests(
                booking_request_status_counts(scope.garage_id, start=period.start, end=period.end)
            )
        ),
    }
    if scope.money:
        result.update(_money(scope, period))
    return result


# --------------------------------------------------------------------------
# Top services (basic)
# --------------------------------------------------------------------------


def top_services(scope: Scope, period: Period) -> dict:
    filters = scope.appointments(period)
    columns = [
        Appointment.appointment_type_id,
        GarageAppointmentType.name,
        func.count().label("appointments"),
        func.count().filter(Appointment.status == COMPLETED).label("completed"),
    ]
    if scope.money:
        columns.append(_sum_price_where(Appointment.status == COMPLETED).label("booked"))
    rows = db.session.execute(
        select(*columns)
        .join(GarageAppointmentType, GarageAppointmentType.id == Appointment.appointment_type_id)
        .where(*filters)
        .group_by(Appointment.appointment_type_id, GarageAppointmentType.name)
        .order_by(func.count().desc(), GarageAppointmentType.name)
        .limit(TOP_LIMIT)
    ).all()

    services = []
    for row in rows:
        entry = {
            "appointment_type_id": row.appointment_type_id,
            "name": row.name,
            "appointments": row.appointments,
            "completed": row.completed,
        }
        if scope.money:
            entry["booked_value"] = to_gbp(to_pence(row.booked))
        services.append(entry)

    # Add-on uptake among bookings that actually happened or are still due -
    # a cancelled booking's add-ons were never sold.
    live = [*filters, Appointment.status != "CANCELLED"]
    add_on_rows = db.session.execute(
        select(
            AppointmentAddOn.name,
            func.count(func.distinct(AppointmentAddOn.appointment_id)).label("appointments"),
            func.sum(AppointmentAddOn.quantity).label("quantity"),
        )
        .join(Appointment, Appointment.id == AppointmentAddOn.appointment_id)
        .where(*live)
        .group_by(AppointmentAddOn.name)
        .order_by(func.count(func.distinct(AppointmentAddOn.appointment_id)).desc())
        .limit(TOP_LIMIT)
    ).all()
    with_add_ons, live_total = db.session.execute(
        select(
            func.count(func.distinct(AppointmentAddOn.appointment_id)),
            select(func.count()).where(*live).scalar_subquery(),
        )
        .select_from(AppointmentAddOn)
        .join(Appointment, Appointment.id == AppointmentAddOn.appointment_id)
        .where(*live)
    ).one()

    return {
        "services": services,
        "add_ons": [
            {"name": name, "appointments": appointments, "quantity": int(quantity or 0)}
            for name, appointments, quantity in add_on_rows
        ],
        "appointments_with_add_ons": int(with_add_ons or 0),
        "add_on_uptake_rate": rate(int(with_add_ons or 0), int(live_total or 0)),
    }


# --------------------------------------------------------------------------
# Advanced sections
# --------------------------------------------------------------------------


def trend(scope: Scope, period: Period) -> dict:
    granularity = period.granularity

    def bucket(column):
        return cast(func.date_trunc(granularity, _local(column, scope)), Date)

    columns = [bucket(Appointment.start_time).label("bucket"), func.count().label("appointments")]
    if scope.money:
        columns.append(_sum_price_where(Appointment.status == COMPLETED).label("booked"))
    appointment_rows = {
        row.bucket: row
        for row in db.session.execute(
            select(*columns).where(*scope.appointments(period)).group_by("bucket")
        ).all()
    }

    visited = _customers_completed_in(scope, period)
    first = _first_completed_visit().where(Appointment.garage_id == scope.garage_id).subquery()
    new_rows = db.session.execute(
        select(bucket(first.c.first_at).label("bucket"), func.count())
        .select_from(visited)
        .join(first, first.c.customer_id == visited.c.customer_id)
        .where(first.c.first_at >= period.start)
        .group_by("bucket")
    ).all()
    new_by_bucket: dict[date, int] = {bucket_start: count for bucket_start, count in new_rows}

    points = []
    for start in period.buckets():
        row = appointment_rows.get(start)
        point: dict = {
            "bucket_start": start,
            "appointments": row.appointments if row else 0,
            "new_customers": new_by_bucket.get(start, 0),
        }
        if scope.money:
            point["booked_value"] = to_gbp(to_pence(row.booked if row else None))
        points.append(point)
    return {"granularity": granularity, "points": points}


def heatmap(scope: Scope, period: Period) -> dict:
    """Appointment starts by local weekday (ISO: 1 = Monday) and hour.
    Cancelled bookings are left out - they never filled the slot. No-shows
    stay in: the slot was held, which is what staffing has to plan for."""
    local_start = _local(Appointment.start_time, scope)
    weekday = cast(extract("isodow", local_start), Integer).label("weekday")
    hour = cast(extract("hour", local_start), Integer).label("hour")
    rows = db.session.execute(
        select(weekday, hour, func.count())
        .where(*scope.appointments(period), Appointment.status != "CANCELLED")
        .group_by(weekday, hour)
        .order_by(weekday, hour)
    ).all()
    cells = [{"weekday": w, "hour": h, "appointments": count} for w, h, count in rows]
    return {"cells": cells, "max": max((c["appointments"] for c in cells), default=0)}


def staff(scope: Scope, period: Period) -> list[dict]:
    rows = db.session.execute(
        select(
            Employee.id,
            Employee.first_name,
            Employee.last_name,
            Employee.email,
            func.count().label("appointments"),
            func.count().filter(Appointment.status == COMPLETED).label("completed"),
            func.count().filter(Appointment.status == "NO_SHOW").label("no_show"),
            func.count().filter(Appointment.status == "CANCELLED").label("cancelled"),
            _sum_price_where(Appointment.status == COMPLETED).label("booked"),
        )
        .join(Employee, Employee.id == Appointment.employee_id)
        .where(*scope.appointments(period))
        .group_by(Employee.id, Employee.first_name, Employee.last_name, Employee.email)
        .order_by(func.count().desc())
    ).all()
    result = []
    for row in rows:
        concluded = row.completed + row.no_show + row.cancelled
        name = " ".join(part for part in (row.first_name, row.last_name) if part)
        result.append(
            {
                "employee_id": row.id,
                "name": name or row.email,
                "appointments": row.appointments,
                "completed": row.completed,
                "no_show": row.no_show,
                "cancelled": row.cancelled,
                "completion_rate": rate(row.completed, concluded),
                "no_show_rate": rate(row.no_show, row.completed + row.no_show),
                "booked_value": to_gbp(to_pence(row.booked)),
            }
        )
    return result


def sources(scope: Scope, period: Period) -> dict[str, int]:
    """Bookings by channel. Every appointment lands in exactly one:

    * ``web`` / ``conversation`` - approved from a booking request, by its
      ``source`` (the public wizard, or the WhatsApp/voice conversation engine);
    * ``queue`` - promoted from the walk-in queue;
    * ``import`` - brought in by a bulk import;
    * ``staff`` - everything else, i.e. created by staff in the app.
    """
    request_source = (
        select(BookingRequest.source)
        .where(BookingRequest.appointment_id == Appointment.id)
        .limit(1)
        .scalar_subquery()
    )
    channel = case(
        (request_source == "WEB", "web"),
        (request_source == "CONVERSATION", "conversation"),
        (exists().where(QueueEntry.appointment_id == Appointment.id), "queue"),
        (Appointment.imported_at.isnot(None), "import"),
        else_="staff",
    ).label("channel")
    rows = db.session.execute(
        select(channel, func.count()).where(*scope.appointments(period)).group_by(channel)
    ).all()
    return {name: count for name, count in rows}


# --------------------------------------------------------------------------
# The whole page
# --------------------------------------------------------------------------


def insights(scope: Scope, period: Period, *, today: date, now: datetime | None = None) -> dict:
    from .due_back import due_back_page

    previous = period.previous(scope.garage)
    current_summary = summary(scope, period)
    payload: dict = {
        "period": {
            "start": period.start_date,
            "end": period.end_date,
            "days": period.days,
            "granularity": period.granularity,
            "timezone": scope.tz_name,
        },
        "previous_period": {
            "start": previous.start_date,
            "end": previous.end_date,
            "days": previous.days,
        },
        "scope": {
            "employee_id": scope.employee_id,
            "money_visible": scope.money,
            "advanced": scope.advanced,
        },
        "summary": current_summary,
        "previous_summary": summary(scope, previous),
        "top_services": top_services(scope, period),
        "sources": sources(scope, period),
    }

    if not scope.advanced:
        payload.update(trend=LOCKED, heatmap=LOCKED, staff=LOCKED, customers=LOCKED, export=LOCKED)
        return payload

    payload["trend"] = trend(scope, period)
    payload["heatmap"] = heatmap(scope, period)
    # A per-employee table is a comparison between colleagues: owners only,
    # and not for a staff member's self-scoped view.
    payload["staff"] = {"rows": staff(scope, period)} if scope.money else None
    payload["customers"] = {
        "new_customers": current_summary["new_customers"],
        "returning_customers": current_summary["returning_customers"],
        "returning_rate": current_summary["returning_rate"],
        "due_back": due_back_page(scope, today=today, page=1, now=now),
    }
    payload["export"] = {"locked": False}
    return payload
