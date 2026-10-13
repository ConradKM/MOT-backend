"""Scheduling rules shared by every path that creates an appointment.

The staff create/edit routes (``app/appointments/routes.py``) and bulk import
(``app/bulk_import/appointments.py``) must agree on what counts as a
double-booking, a full slot, a valid status and a derived end time. These are
the predicates both use. They answer the question and never abort: the routes
turn a "yes" into a 409/422, while an import reports it per row as a warning
the user may override.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.appointments.statuses.defaults import DEFAULT_STATUS_KEYS
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_status import GarageAppointmentStatus
from app.public_booking.availability import slot_capacity_usage


def allowed_statuses(garage_id) -> set[str]:
    """The garage's configured status keys, or the built-in default set if it
    hasn't customised them."""
    configured = {
        key
        for (key,) in db.session.query(GarageAppointmentStatus.key)
        .filter_by(garage_id=garage_id)
        .all()
    }
    return configured or set(DEFAULT_STATUS_KEYS)


def default_end_time(start_time: datetime, appointment_type, add_on_minutes: int = 0):
    """``start_time`` plus the service's default duration (and any add-ons),
    or ``None`` when the service has no default duration to derive one from."""
    if appointment_type.default_duration_minutes is None:
        return None
    return start_time + timedelta(
        minutes=appointment_type.default_duration_minutes + add_on_minutes
    )


def has_employee_conflict(
    employee_id, start_time: datetime, end_time: datetime, exclude_appointment_id=None
) -> bool:
    """Whether ``employee_id`` already has a live appointment overlapping the
    range. Cancelled appointments never conflict."""
    query = Appointment.query.filter(
        Appointment.employee_id == employee_id,
        Appointment.status != "CANCELLED",
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
    )

    if exclude_appointment_id is not None:
        query = query.filter(Appointment.id != exclude_appointment_id)

    return query.first() is not None


def capacity_exceeded(
    garage, start_time: datetime, end_time: datetime, exclude_appointment_id=None
) -> bool:
    """Apply the garage-wide capacity contract to staff-created schedules.

    Employee conflict checks alone are insufficient when a business has set
    ``capacity_per_slot`` below its number of staff.  This deliberately uses
    the same reservation accounting as public booking and request approval,
    including pending customer requests, so staff actions cannot silently
    take a slot those flows have already reserved.
    """
    duration_minutes = int((end_time - start_time).total_seconds() // 60)
    used, capacity = slot_capacity_usage(
        garage,
        start_time.date(),
        start_time,
        duration_minutes,
        exclude_appointment_id=exclude_appointment_id,
        # Walk-in reserved windows protect bays from *public* booking only.
        include_walkin_reservations=False,
    )
    return used >= capacity
