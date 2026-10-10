"""Filter resolution for calendar exports - the single source of truth.

A one-off .ics download and a subscription feed both describe "whose
appointments, which types, which days", and both go through the functions
here, so the file someone downloads and the feed they subscribe to always
mean the same thing. Permissions are evaluated against the *requesting
identity as it is now*: the signed-in employee for a download or a feed
create, and the feed's creator, re-read on every fetch, for a subscription.

Semantics follow GET /api/appointments (app/appointments/routes.py): dates
name business-local calendar days, matched against an appointment's
start_time, and every query is pinned to one garage_id. On top of that,
cancelled and no-show appointments are never exported - for a subscription
they simply drop out of the feed, which removes them from the calendar.
"""

import uuid
from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime, timedelta

from flask import current_app
from sqlalchemy.orm import joinedload

from app.garages.timezones import local_day_bounds_utc, local_day_for
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.calendar_feed import CalendarFeed
from app.models.employee import Employee
from app.models.garage import GARAGE_STATUS_ARCHIVED, GARAGE_STATUS_SUSPENDED, Garage

OWNER_ROLE = "OWNER"
EXCLUDED_STATUSES = ("CANCELLED", "NO_SHOW")

# None means "every worker" / "every appointment type" (including ones added
# later); a frozenset is an explicit selection, which may legitimately end up
# empty once a selected row is deleted and then simply matches nothing.
EmployeeSelection = frozenset[uuid.UUID] | None
TypeSelection = frozenset[uuid.UUID] | None


class CalendarExportError(Exception):
    """A filter the requester may not use (403) or that can't be served (422).

    Routes turn this into an HTTP error; a feed fetch turns any of it into the
    feed's single opaque 404.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def is_owner(employee: Employee) -> bool:
    return employee.has_role(OWNER_ROLE)


def resolve_employee_ids(
    requester: Employee,
    scope: str,
    employee_ids: Iterable[uuid.UUID] = (),
    *,
    require_active: bool = True,
) -> EmployeeSelection:
    """Whose appointments ``requester`` is exporting, or a CalendarExportError.

    An OWNER may export everyone (``ALL``) or any of the garage's employees;
    anyone else only ever their own appointments, whatever scope they ask for.
    ``require_active`` mirrors the calendar page's worker filter, which only
    offers active employees - it is relaxed when re-checking a stored feed so
    a selected worker being deactivated narrows nothing that was allowed.
    """
    if scope == "ME":
        return frozenset({requester.id})

    if scope == "ALL":
        if not is_owner(requester):
            raise CalendarExportError(403, "Only an owner can export every worker's calendar.")
        return None

    selected = frozenset(employee_ids)
    if not selected:
        raise CalendarExportError(422, "Choose at least one worker.")

    found = Employee.query.filter(
        Employee.id.in_(selected), Employee.garage_id == requester.garage_id
    ).all()
    if len(found) != len(selected):
        raise CalendarExportError(422, "employee_ids must belong to your business.")

    if not is_owner(requester) and selected != {requester.id}:
        raise CalendarExportError(403, "You can only export your own appointments.")

    if require_active and any(not employee.is_active for employee in found):
        raise CalendarExportError(422, "A selected employee's account is deactivated.")

    return selected


def resolve_appointment_type_ids(
    garage_id: uuid.UUID, appointment_type_ids: Collection[uuid.UUID] | None
) -> TypeSelection:
    """None (every type) or the explicit selection, all within ``garage_id``.

    Hidden and deprecated types are allowed: they only block *new* bookings,
    and appointments that already use them still belong on a calendar.
    """
    if appointment_type_ids is None:
        return None

    selected = frozenset(appointment_type_ids)
    if not selected:
        raise CalendarExportError(422, "Choose at least one appointment type.")

    found = GarageAppointmentType.query.filter(
        GarageAppointmentType.id.in_(selected), GarageAppointmentType.garage_id == garage_id
    ).count()
    if found != len(selected):
        raise CalendarExportError(422, "appointment_type_ids must belong to your business.")

    return selected


def validate_date_range(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise CalendarExportError(422, "start_date must be on or before end_date.")

    max_days = current_app.config["CALENDAR_EXPORT_MAX_DAYS"]
    if (end_date - start_date).days + 1 > max_days:
        raise CalendarExportError(
            422,
            f"The date range can be at most {max_days} days. Choose a shorter range, "
            "or export it in parts.",
        )


def feed_date_range(garage: Garage, feed: CalendarFeed, now: datetime) -> tuple[date, date]:
    """A feed's rolling window, anchored on today in the garage's timezone."""
    today = local_day_for(garage, now)
    return today - timedelta(days=feed.days_back), today + timedelta(days=feed.days_forward)


def appointments_for_export(
    garage: Garage,
    employee_ids: EmployeeSelection,
    appointment_type_ids: TypeSelection,
    start_date: date,
    end_date: date,
) -> list[Appointment]:
    """The appointments a validated filter covers, capped at
    CALENDAR_EXPORT_MAX_EVENTS (422 above it - never a truncated calendar).

    Only the relationships an event is built from are loaded: the customer is
    never touched, so no customer detail can reach a calendar by accident.
    """
    range_start, _ = local_day_bounds_utc(garage, start_date)
    _, range_end = local_day_bounds_utc(garage, end_date)

    query = Appointment.query.filter(
        Appointment.garage_id == garage.id,
        Appointment.status.notin_(EXCLUDED_STATUSES),
        Appointment.start_time >= range_start,
        Appointment.start_time <= range_end,
    )
    if employee_ids is not None:
        query = query.filter(Appointment.employee_id.in_(employee_ids))
    if appointment_type_ids is not None:
        query = query.filter(Appointment.appointment_type_id.in_(appointment_type_ids))

    max_events = current_app.config["CALENDAR_EXPORT_MAX_EVENTS"]
    count = query.count()
    if count > max_events:
        raise CalendarExportError(
            422,
            f"This would export {count} appointments; the limit is {max_events}. "
            "Narrow the date range, workers or appointment types.",
        )

    appointments: list[Appointment] = (
        query.options(
            joinedload(Appointment.employee),
            joinedload(Appointment.appointment_type),
            joinedload(Appointment.vehicle),
        )
        .order_by(Appointment.start_time, Appointment.id)
        .all()
    )
    return appointments


def shows_worker_names(employee_ids: EmployeeSelection) -> bool:
    """More than one worker's calendar in one export: name each event's worker
    so overlapping appointments can be told apart."""
    return employee_ids is None or len(employee_ids) > 1


# --------------------------------------------------------------------------
# Subscription feeds
# --------------------------------------------------------------------------


def feed_employee_ids(feed: CalendarFeed) -> EmployeeSelection:
    """Re-resolve a stored feed against its creator's authority *now*.

    Raises CalendarExportError when the feed may no longer be served: the
    creator was deactivated or moved out of the garage, the garage is
    suspended or archived, or the creator lost the OWNER role while the feed
    covers anyone but themselves. A feed never outlives the permissions it
    was created under.
    """
    creator = feed.created_by
    if creator is None or not creator.is_active or creator.garage_id != feed.garage_id:
        raise CalendarExportError(404, "Feed creator can no longer export.")

    if feed.garage.status in (GARAGE_STATUS_SUSPENDED, GARAGE_STATUS_ARCHIVED):
        raise CalendarExportError(404, "Garage is not active.")

    return resolve_employee_ids(
        creator,
        feed.scope,
        (employee.id for employee in feed.employees),
        require_active=False,
    )


def feed_appointment_type_ids(feed: CalendarFeed) -> TypeSelection:
    if feed.all_appointment_types:
        return None
    return frozenset(appointment_type.id for appointment_type in feed.appointment_types)


def active_feed_count(employee: Employee) -> int:
    count: int = CalendarFeed.query.filter_by(
        created_by_employee_id=employee.id, revoked_at=None
    ).count()
    return count


def utcnow() -> datetime:
    return datetime.now(UTC)
