"""Appointments: parse, match, create - importing an existing diary.

Mirrors the staff create path (``POST /api/appointments/``) through the same
helpers - service lookup, end-time derivation, price snapshot, checklist
snapshot, and the employee-conflict and capacity predicates in
``app/appointments/scheduling.py`` - with two deliberate differences:

* **Clashes are warnings, not refusals.** A real diary can be legitimately
  overbooked, and the AI that read the screenshot can misread a time, so a
  double-booking, a full slot or a time outside opening hours is reported on
  the row for a human to accept ("import anyway") or fix. Anything that makes
  the row meaningless - an unknown service, an unparseable or nonexistent
  time, an end before the start, an unknown status - is an error, and nothing
  imports until it is fixed.
* **No side effects.** An import records bookings that already exist; it is
  not a customer booking now. It emits no ``APPOINTMENT_CREATED`` (so no
  confirmation email/SMS and no conversation hooks), and every row is stamped
  ``imported_at``, which the reminder sender skips (see
  ``Appointment.imported_at``).

Re-running is safe: a row matching an existing appointment (same service,
same start, same customer - by phone, else email, else exact name) is
reported ``EXISTS`` and skipped, and that check runs *before* any customer is
created, so a re-run never makes a second copy of a customer.

Customer data is personal data from a screenshot, via an LLM: it is validated
strictly, stored as plain text, and never logged.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from typing import Any

from app.appointments.add_ons.service import total_price
from app.appointments.checklists.service import snapshot_checklist_for_appointment
from app.appointments.scheduling import (
    allowed_statuses,
    capacity_exceeded,
    default_end_time,
    has_employee_conflict,
)
from app.employees.service import email_format_error
from app.garages.timezones import timezone_for
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile, normalize_uk_phone
from app.public_booking.availability import open_interval

from .result import (
    MAX_APPOINTMENTS,
    STATUS_EXISTS,
    ImportResult,
    ItemResult,
    read_rows,
    text_field,
    unknown_key_warnings,
)
from .services import name_key

APPOINTMENT_FIELDS = {
    "service",
    "start",
    "end",
    "status",
    "employee_email",
    "customer",
    "notes",
}
CUSTOMER_FIELDS = {"first_name", "last_name", "phone", "email"}

#: Column sizes on ``customers`` / ``garage_appointment_types``.
SERVICE_NAME_MAX = 100
CUSTOMER_NAME_MAX = 100
CUSTOMER_EMAIL_MAX = 320
CUSTOMER_PHONE_MAX = 40
#: The same cap the public booking form puts on notes.
NOTES_MAX = 2000

FLAG_PAST = "past"
FLAG_CLASH = "clash"
FLAG_OVER_CAPACITY = "over_capacity"
FLAG_OUT_OF_HOURS = "out_of_hours"


# --------------------------------------------------------------------------
# Field parsing (pure)
# --------------------------------------------------------------------------


class TimeError(ValueError):
    """A start/end value that cannot be turned into one instant."""


def parse_wall_clock(value: Any, tz: tzinfo) -> datetime:
    """An import time as an aware UTC instant.

    ``"2026-11-03T09:30"`` is a wall-clock time in the business's timezone;
    a value with an explicit offset (``+01:00`` or ``Z``) is taken as given.
    A wall-clock time that never happens (clocks going forward) or happens
    twice (clocks going back) is refused rather than guessed - the fix is to
    give the offset explicitly.
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeError('Expected a date and time like "2026-11-03T09:30".')
    text = value.strip()
    if len(text) <= 10:
        raise TimeError(f'{text!r} needs a time as well as a date, e.g. "2026-11-03T09:30".')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeError(
            f'{text!r} is not a date and time - use the form "2026-11-03T09:30".'
        ) from exc
    parsed = parsed.replace(microsecond=0)

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    zone_name = getattr(tz, "key", str(tz))
    first = parsed.replace(tzinfo=tz, fold=0)
    round_trip = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != parsed:
        raise TimeError(
            f"{text!r} does not exist in {zone_name} - the clocks go forward then. "
            "Check the time, or give it with an explicit UTC offset."
        )
    if first.utcoffset() != parsed.replace(tzinfo=tz, fold=1).utcoffset():
        raise TimeError(
            f"{text!r} happens twice in {zone_name} - the clocks go back then. "
            'Give it with an explicit UTC offset, e.g. "+01:00" or "+00:00".'
        )
    return first.astimezone(UTC)


def normalise_phone(raw: str) -> tuple[str, bool]:
    """``(E.164, is_mobile)``. A mobile is what the public booking form
    accepts (``UKMobileField``); a valid landline is accepted too, since a
    diary records how the business reached the customer, but flagged."""
    try:
        return normalize_uk_mobile(raw), True
    except InvalidPhoneNumberError as mobile_error:
        try:
            return normalize_uk_phone(raw), False
        except InvalidPhoneNumberError:
            raise mobile_error from None


def _stored_phone_key(raw: str | None) -> str | None:
    """A comparable key for a phone already on a customer record. Staff-typed
    numbers are free text, so normalise where possible and fall back to the
    digits."""
    if not raw:
        return None
    try:
        return normalize_uk_phone(raw)
    except InvalidPhoneNumberError:
        digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
        return digits or None


# --------------------------------------------------------------------------
# Lookups (one query each, per import)
# --------------------------------------------------------------------------


@dataclass
class _CustomerInput:
    first_name: str
    last_name: str
    phone: str | None
    email: str | None

    def same_person(self, customer: Customer) -> bool:
        """The duplicate-appointment test: phone, else email, else exact name.
        Name alone is acceptable here only because service and start time
        must match too, and the failure mode is skipping a row."""
        if self.phone:
            return _stored_phone_key(customer.phone) == self.phone
        if self.email:
            return (customer.email or "").strip().casefold() == self.email.casefold()
        return (
            customer.first_name.strip().casefold() == self.first_name.casefold()
            and customer.last_name.strip().casefold() == self.last_name.casefold()
        )


class _Directory:
    """The business's services, team and customers, loaded once per import."""

    def __init__(self, garage, session) -> None:
        self.garage = garage
        self.session = session
        self.types = {
            name_key(t.name): t
            for t in session.query(GarageAppointmentType)
            .filter(GarageAppointmentType.garage_id == garage.id)
            .all()
        }
        employees = (
            session.query(Employee)
            .filter(Employee.garage_id == garage.id)
            .order_by(Employee.created_at, Employee.id)
            .all()
        )
        self.employees = {e.email.strip().casefold(): e for e in employees}
        self.owner = next(
            (e for e in employees if e.is_active and e.has_role("OWNER")),
            None,
        )
        self.statuses = allowed_statuses(garage.id)

        # Linking is by phone, else email - never by name, which would merge
        # strangers. Oldest record wins when several share a number.
        self.by_phone: dict[str, Customer] = {}
        self.by_email: dict[str, Customer] = {}
        for customer in (
            session.query(Customer)
            .filter(Customer.garage_id == garage.id)
            .order_by(Customer.created_at, Customer.id)
            .all()
        ):
            phone_key = _stored_phone_key(customer.phone)
            if phone_key:
                self.by_phone.setdefault(phone_key, customer)
            if customer.email:
                self.by_email.setdefault(customer.email.strip().casefold(), customer)
        #: Customers this import created, to the row index that created them.
        self.created_by_row: dict[Any, int] = {}

    def service_suggestions(self, name: str) -> list[str]:
        names = [t.name for t in self.types.values()]
        return difflib.get_close_matches(name, names, n=3, cutoff=0.5)

    def find_customer(self, data: _CustomerInput) -> Customer | None:
        if data.phone and data.phone in self.by_phone:
            return self.by_phone[data.phone]
        if data.email and data.email.casefold() in self.by_email:
            return self.by_email[data.email.casefold()]
        return None

    def remember(self, customer: Customer, index: int) -> None:
        self.created_by_row[customer.id] = index
        if customer.phone:
            self.by_phone.setdefault(customer.phone, customer)
        if customer.email:
            self.by_email.setdefault(customer.email.casefold(), customer)


# --------------------------------------------------------------------------
# One row
# --------------------------------------------------------------------------


def _local_iso(instant: datetime, tz: tzinfo) -> str:
    return instant.astimezone(tz).isoformat()


def _parse_customer(item: ItemResult, raw: Any, path: str) -> _CustomerInput | None:
    if not isinstance(raw, dict):
        item.error(path, "Expected an object with at least 'first_name' and 'last_name'.")
        return None
    unknown_key_warnings(item, raw, CUSTOMER_FIELDS, path)

    first = text_field(item, raw, "first_name", path, max_length=CUSTOMER_NAME_MAX, required=True)
    last = text_field(item, raw, "last_name", path, max_length=CUSTOMER_NAME_MAX, required=True)
    email = text_field(item, raw, "email", path, max_length=CUSTOMER_EMAIL_MAX)
    if email is not None and email_format_error(email):
        item.error(f"{path}.email", "Not a valid email address - fix it or use null.")
        email = None

    phone = None
    raw_phone = text_field(item, raw, "phone", path, max_length=CUSTOMER_PHONE_MAX)
    if raw_phone is not None:
        try:
            phone, is_mobile = normalise_phone(raw_phone)
        except InvalidPhoneNumberError as exc:
            item.error(f"{path}.phone", f"{exc} Fix it or use null.")
        else:
            if not is_mobile:
                item.warn(
                    f"{path}.phone",
                    "A landline: it is saved, but text messages cannot reach it.",
                )

    if first is None or last is None:
        return None
    return _CustomerInput(first_name=first, last_name=last, phone=phone, email=email)


def _find_duplicate(directory: _Directory, appointment_type, start, customer: _CustomerInput):
    candidates = (
        directory.session.query(Appointment)
        .filter(
            Appointment.garage_id == directory.garage.id,
            Appointment.appointment_type_id == appointment_type.id,
            Appointment.start_time == start,
        )
        .all()
    )
    return next((a for a in candidates if customer.same_person(a.customer)), None)


def _import_row(
    directory: _Directory,
    raw: Any,
    index: int,
    tz: tzinfo,
    now: datetime,
    created_rows: dict[Any, int],
) -> ItemResult:
    item = ItemResult(index=index)
    path = f"appointments[{index}]"
    if not isinstance(raw, dict):
        item.error(path, "Expected an object with at least 'service', 'start' and 'customer'.")
        return item

    if "add_ons" in raw:
        item.warn(f"{path}.add_ons", "Add-ons are not imported; ignored.")
    unknown_key_warnings(item, raw, APPOINTMENT_FIELDS | {"add_ons"}, path)
    normalised: dict[str, Any] = {"flags": []}
    item.normalised = normalised

    # --- service -------------------------------------------------------
    appointment_type = None
    service_name = text_field(
        item, raw, "service", path, max_length=SERVICE_NAME_MAX, required=True
    )
    if service_name is not None:
        appointment_type = directory.types.get(name_key(service_name))
        if appointment_type is None:
            suggestions = directory.service_suggestions(service_name)
            hint = (
                f" Did you mean {', '.join(repr(s) for s in suggestions)}?"
                if suggestions
                else " Add it as a service first, or use one of the business's service names."
            )
            item.error(f"{path}.service", f"No service called {service_name!r}.{hint}")
        elif appointment_type.status != "ACTIVE":
            item.error(
                f"{path}.service",
                f"{appointment_type.name!r} is not active and cannot be used for new bookings.",
            )
        else:
            normalised["service"] = appointment_type.name
            normalised["service_id"] = str(appointment_type.id)

    # --- times ---------------------------------------------------------
    start = end = None
    try:
        start = parse_wall_clock(raw.get("start"), tz)
    except TimeError as exc:
        item.error(f"{path}.start", str(exc))
    if raw.get("end") is not None:
        try:
            end = parse_wall_clock(raw.get("end"), tz)
        except TimeError as exc:
            item.error(f"{path}.end", str(exc))
    elif start is not None and appointment_type is not None and "service" in normalised:
        end = default_end_time(start, appointment_type)
        if end is None:
            item.error(
                f"{path}.end",
                f"Required - {appointment_type.name!r} has no default duration to work it out from.",
            )
    if start is not None and end is not None and end <= start:
        item.error(f"{path}.end", "Must be after the start.")
        end = None
    if start is not None:
        normalised["start"] = _local_iso(start, tz)
        if start < now:
            normalised["flags"].append(FLAG_PAST)
    if end is not None:
        normalised["end"] = _local_iso(end, tz)

    # --- status --------------------------------------------------------
    status = "BOOKED"
    raw_status = raw.get("status")
    if raw_status is not None:
        if not isinstance(raw_status, str) or not raw_status.strip():
            item.error(f"{path}.status", f"Must be one of {sorted(directory.statuses)}.")
        else:
            status = raw_status.strip().upper().replace(" ", "_")
            if status not in directory.statuses:
                item.error(
                    f"{path}.status",
                    f"{raw_status.strip()!r} is not a status this business uses. "
                    f"Use one of {sorted(directory.statuses)}, or leave it out for BOOKED.",
                )
    normalised["status"] = status

    # --- employee ------------------------------------------------------
    employee = None
    employee_email = text_field(item, raw, "employee_email", path, max_length=CUSTOMER_EMAIL_MAX)
    if employee_email is not None:
        employee = directory.employees.get(employee_email.casefold())
        if employee is None:
            item.error(f"{path}.employee_email", "No team member with this email in the business.")
        elif not employee.is_active:
            item.error(f"{path}.employee_email", "This team member's account is deactivated.")
            employee = None
    else:
        employee = directory.owner
        if employee is None:
            item.error(f"{path}.employee_email", "Required - the business has no active owner.")
    if employee is not None:
        normalised["employee"] = {
            "email": employee.email,
            "name": " ".join(p for p in (employee.first_name, employee.last_name) if p) or None,
        }

    # --- customer and notes --------------------------------------------
    customer_input = _parse_customer(item, raw.get("customer"), f"{path}.customer")
    if customer_input is not None:
        normalised["customer"] = {
            "first_name": customer_input.first_name,
            "last_name": customer_input.last_name,
            "phone": customer_input.phone,
            "email": customer_input.email,
        }
    notes = text_field(item, raw, "notes", path, max_length=NOTES_MAX)
    normalised["notes"] = notes

    if item.errors:
        return item
    assert appointment_type is not None and start is not None and end is not None
    assert employee is not None and customer_input is not None

    price = total_price(appointment_type.base_price, Decimal("0.00"))
    normalised["price_at_booking"] = None if price is None else str(price)

    # --- already there? (before any customer is created) ---------------
    duplicate = _find_duplicate(directory, appointment_type, start, customer_input)
    if duplicate is not None:
        item.status = STATUS_EXISTS
        if duplicate.id in created_rows:
            normalised["duplicate_of"] = created_rows[duplicate.id]
            item.warn(path, f"Same as appointments[{created_rows[duplicate.id]}]; skipped.")
        else:
            normalised["existing_id"] = str(duplicate.id)
        return item

    # --- clashes: warnings, through the staff path's own predicates ----
    if status != "CANCELLED":
        if has_employee_conflict(employee.id, start, end):
            normalised["flags"].append(FLAG_CLASH)
            item.warn(
                f"{path}.start",
                f"{normalised['employee']['name'] or employee.email} already has an "
                "appointment during this time.",
            )
        if capacity_exceeded(directory.garage, start, end):
            normalised["flags"].append(FLAG_OVER_CAPACITY)
            item.warn(f"{path}.start", "The business is already fully booked at this time.")
        local_day = start.astimezone(tz).date()
        hours = open_interval(directory.garage, local_day)
        if hours is None:
            normalised["flags"].append(FLAG_OUT_OF_HOURS)
            item.warn(f"{path}.start", "The business is closed on this day.")
        elif start < hours[0] or end > hours[1]:
            normalised["flags"].append(FLAG_OUT_OF_HOURS)
            item.warn(f"{path}.start", "Outside the business's opening hours.")

    # --- customer: link by phone/email, else create --------------------
    customer = directory.find_customer(customer_input)
    customer_summary = normalised["customer"]
    if customer is None:
        customer = Customer(
            garage_id=directory.garage.id,
            first_name=customer_input.first_name,
            last_name=customer_input.last_name,
            phone=customer_input.phone,
            email=customer_input.email,
        )
        directory.session.add(customer)
        directory.session.flush()
        directory.remember(customer, index)
        customer_summary["match"] = "new"
    elif customer.id in directory.created_by_row:
        customer_summary["match"] = "earlier_row"
        customer_summary["same_as_index"] = directory.created_by_row[customer.id]
    else:
        customer_summary["match"] = "existing"
        customer_summary["existing_id"] = str(customer.id)
        customer_summary["existing_name"] = f"{customer.first_name} {customer.last_name}"
        # An archived customer booked again is active again - as on the
        # staff path (app/appointments/routes.py::_get_owned_customer).
        if not customer.is_active:
            customer.is_active = True

    appointment = Appointment(
        garage_id=directory.garage.id,
        employee_id=employee.id,
        customer_id=customer.id,
        vehicle_id=None,
        start_time=start,
        end_time=end,
        appointment_type_id=appointment_type.id,
        status=status,
        notes=notes,
        price_at_booking=price,
        appointment_type_name_at_booking=appointment_type.name,
        imported_at=now,
    )
    directory.session.add(appointment)
    directory.session.flush()
    snapshot_checklist_for_appointment(appointment)
    created_rows[appointment.id] = index
    return item


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def import_appointment_rows(
    garage, rows: list[Any], session, *, now: datetime | None = None
) -> ImportResult:
    """Validate and create ``rows`` for ``garage``, in order.

    Runs in the caller's transaction and only flushes: the caller commits, or
    rolls back for a preview - so a preview is this exact function. Rows are
    applied one at a time, so a later row sees the earlier ones (a diary that
    double-books someone within the paste is flagged too).
    """
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    tz = timezone_for(garage)
    directory = _Directory(garage, session)
    created_rows: dict[Any, int] = {}

    result = ImportResult(
        kind="appointments",
        context={"timezone": getattr(tz, "key", str(tz))},
    )
    for index, raw in enumerate(rows):
        result.items.append(_import_row(directory, raw, index, tz, now, created_rows))
    return result


def import_appointments(garage, payload: Any, session, *, now: datetime | None = None):
    """Validate a pasted ``{"appointments": [...]}`` and create the new ones."""
    rows, payload_warnings = read_rows(payload, "appointments", MAX_APPOINTMENTS)
    result = import_appointment_rows(garage, rows, session, now=now)
    result.warnings = payload_warnings + result.warnings
    return result
