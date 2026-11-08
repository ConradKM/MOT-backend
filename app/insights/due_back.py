"""Customers who are due back for a service.

Driven entirely by each service's own settings on
``GarageAppointmentType`` - never by its name:

* ``return_interval_days`` - due on (local date of the customer's last
  COMPLETED visit for that service) + the interval. Keyed per customer and
  service.
* ``due_back_uses_mot_expiry`` - due on the vehicle's stored
  ``Vehicle.mot_expiry_date`` (what MOT reminders maintain), keyed per
  customer, service and vehicle so a two-car household gets two entries.

A service with neither produces nothing. An entry shows once its due date
is within :data:`DUE_SOON_DAYS` of today ("due soon") or has passed ("due"),
and is suppressed while the customer has any upcoming booking - they are
already coming back, so there is nobody to chase.

Unlike the rest of Insights this is "as of today", not tied to the chosen
date range: who is due back doesn't depend on which month you're looking at.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Date, String, cast, exists, func, literal, select, union_all

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.vehicle import Vehicle

from .service import COMPLETED, Scope

DUE_SOON_DAYS = 14
DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100

#: A future appointment in one of these states means the customer is already
#: booked in.
UPCOMING_STATUSES = ("REQUESTED", "BOOKED", "IN_PROGRESS", "ACTION_NEEDED")


def _entries(scope: Scope):
    """One row per due-back candidate: who, for what, last visit, due date."""
    local_day = cast(func.timezone(scope.tz_name, Appointment.start_time), Date)
    base = [
        Appointment.garage_id == scope.garage_id,
        Appointment.status == COMPLETED,
    ]

    # DISTINCT ON keeps each group's latest completed visit - and with it the
    # employee who saw them, which is what scopes the list for staff.
    last_interval_visit = (
        select(
            Appointment.customer_id,
            Appointment.appointment_type_id,
            Appointment.vehicle_id,
            Appointment.employee_id,
            local_day.label("last_visit"),
            GarageAppointmentType.return_interval_days.label("interval_days"),
        )
        .join(GarageAppointmentType, GarageAppointmentType.id == Appointment.appointment_type_id)
        .where(
            *base,
            GarageAppointmentType.return_interval_days.isnot(None),
            GarageAppointmentType.due_back_uses_mot_expiry.is_(False),
        )
        .distinct(Appointment.customer_id, Appointment.appointment_type_id)
        .order_by(
            Appointment.customer_id,
            Appointment.appointment_type_id,
            Appointment.start_time.desc(),
        )
        .subquery()
    )
    by_interval = select(
        last_interval_visit.c.customer_id,
        last_interval_visit.c.appointment_type_id,
        last_interval_visit.c.vehicle_id,
        last_interval_visit.c.employee_id,
        last_interval_visit.c.last_visit,
        (last_interval_visit.c.last_visit + last_interval_visit.c.interval_days).label("due_date"),
        cast(literal("interval"), String).label("basis"),
    )

    last_mot_visit = (
        select(
            Appointment.customer_id,
            Appointment.appointment_type_id,
            Appointment.vehicle_id,
            Appointment.employee_id,
            local_day.label("last_visit"),
        )
        .join(GarageAppointmentType, GarageAppointmentType.id == Appointment.appointment_type_id)
        .where(
            *base,
            Appointment.vehicle_id.isnot(None),
            GarageAppointmentType.due_back_uses_mot_expiry.is_(True),
        )
        .distinct(Appointment.customer_id, Appointment.appointment_type_id, Appointment.vehicle_id)
        .order_by(
            Appointment.customer_id,
            Appointment.appointment_type_id,
            Appointment.vehicle_id,
            Appointment.start_time.desc(),
        )
        .subquery()
    )
    by_mot_expiry = (
        select(
            last_mot_visit.c.customer_id,
            last_mot_visit.c.appointment_type_id,
            last_mot_visit.c.vehicle_id,
            last_mot_visit.c.employee_id,
            last_mot_visit.c.last_visit,
            Vehicle.mot_expiry_date.label("due_date"),
            cast(literal("mot_expiry"), String).label("basis"),
        )
        .join(Vehicle, Vehicle.id == last_mot_visit.c.vehicle_id)
        .where(
            Vehicle.mot_expiry_date.isnot(None),
            Vehicle.is_active.is_(True),
            # A car since sold to another customer is no longer theirs to book.
            Vehicle.customer_id == last_mot_visit.c.customer_id,
        )
    )
    return union_all(by_interval, by_mot_expiry).subquery()


def _filtered(scope: Scope, *, today: date, now: datetime):
    entries = _entries(scope)
    booked_in = exists().where(
        Appointment.garage_id == scope.garage_id,
        Appointment.customer_id == entries.c.customer_id,
        Appointment.start_time >= now,
        Appointment.status.in_(UPCOMING_STATUSES),
    )
    filters = [entries.c.due_date <= today + timedelta(days=DUE_SOON_DAYS), ~booked_in]
    if scope.employee_id is not None:
        filters.append(entries.c.employee_id == scope.employee_id)
    return entries, filters


def due_back_page(
    scope: Scope,
    *,
    today: date,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    page = max(1, page)
    entries, filters = _filtered(scope, today=today, now=now)

    due, due_soon = db.session.execute(
        select(
            func.count().filter(entries.c.due_date <= today),
            func.count().filter(entries.c.due_date > today),
        ).where(*filters)
    ).one()
    due, due_soon = int(due or 0), int(due_soon or 0)

    rows = db.session.execute(
        select(
            entries.c.customer_id,
            Customer.first_name,
            Customer.last_name,
            entries.c.appointment_type_id,
            GarageAppointmentType.name.label("service_name"),
            entries.c.vehicle_id,
            Vehicle.registration_number.label("registration"),
            entries.c.last_visit,
            entries.c.due_date,
            entries.c.basis,
        )
        .join(Customer, Customer.id == entries.c.customer_id)
        .join(GarageAppointmentType, GarageAppointmentType.id == entries.c.appointment_type_id)
        .outerjoin(Vehicle, Vehicle.id == entries.c.vehicle_id)
        .where(*filters)
        # Soonest-due first, then the most recently lapsed: the people most
        # likely to come back if asked now. Long-lapsed customers sink.
        .order_by(entries.c.due_date.desc(), Customer.last_name, Customer.first_name)
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    return {
        "as_of": today,
        "due_soon_days": DUE_SOON_DAYS,
        "due": due,
        "due_soon": due_soon,
        "total": due + due_soon,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                "customer_id": row.customer_id,
                "customer_name": f"{row.first_name} {row.last_name}".strip(),
                "appointment_type_id": row.appointment_type_id,
                "service_name": row.service_name,
                "vehicle_id": row.vehicle_id,
                "vehicle_registration": row.registration,
                "last_visit": row.last_visit,
                "due_date": row.due_date,
                "status": "due" if row.due_date <= today else "due_soon",
                "basis": row.basis,
            }
            for row in rows
        ],
    }
