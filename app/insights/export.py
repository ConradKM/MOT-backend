"""CSV export of the appointments behind an Insights period.

Columns follow the same :class:`~app.insights.service.Scope` rules as the
page: staff get their own rows and no money; customer contact details
(email, phone) are owners only. Cells that a spreadsheet would read as a
formula are prefixed with ``'`` (CSV injection) - customer names and notes
are typed by the public.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import func, select

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.payments.payment import PAYMENT_STATUSES_CHARGED, BookingPayment

from .periods import Period
from .service import Scope, to_gbp

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_filename(scope: Scope, period: Period) -> str:
    return f"insights-{scope.garage.slug}-{period.start_date}-to-{period.end_date}.csv"


def export_csv(scope: Scope, period: Period) -> str:
    local_start = func.timezone(scope.tz_name, Appointment.start_time)
    deposits = (
        select(
            BookingPayment.appointment_id.label("appointment_id"),
            func.sum(
                BookingPayment.amount_minor - func.coalesce(BookingPayment.refunded_amount_minor, 0)
            ).label("pence"),
        )
        .where(
            BookingPayment.garage_id == scope.garage_id,
            BookingPayment.status.in_(PAYMENT_STATUSES_CHARGED),
            BookingPayment.appointment_id.isnot(None),
        )
        .group_by(BookingPayment.appointment_id)
        .subquery()
    )

    rows = db.session.execute(
        select(
            local_start.label("local_start"),
            func.coalesce(
                Appointment.appointment_type_name_at_booking, GarageAppointmentType.name
            ).label("service"),
            Employee.first_name.label("staff_first"),
            Employee.last_name.label("staff_last"),
            Employee.email.label("staff_email"),
            Appointment.status,
            Customer.first_name.label("customer_first"),
            Customer.last_name.label("customer_last"),
            Customer.email.label("customer_email"),
            Customer.phone.label("customer_phone"),
            Appointment.price_at_booking,
            Appointment.discount_code,
            Appointment.discount_amount,
            deposits.c.pence.label("deposit_pence"),
        )
        .join(GarageAppointmentType, GarageAppointmentType.id == Appointment.appointment_type_id)
        .join(Employee, Employee.id == Appointment.employee_id)
        .join(Customer, Customer.id == Appointment.customer_id)
        .outerjoin(deposits, deposits.c.appointment_id == Appointment.id)
        .where(*scope.appointments(period))
        .order_by(Appointment.start_time)
    ).all()

    header = ["Date", "Time", "Service", "Staff", "Status", "Customer"]
    if scope.money:
        header += ["Email", "Phone", "Price (GBP)", "Discount code", "Discount (GBP)"]
        header += ["Deposit (GBP)"]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        staff_name = " ".join(p for p in (row.staff_first, row.staff_last) if p) or row.staff_email
        cells = [
            row.local_start.date().isoformat(),
            row.local_start.strftime("%H:%M"),
            row.service,
            staff_name,
            row.status,
            f"{row.customer_first} {row.customer_last}".strip(),
        ]
        if scope.money:
            cells += [
                row.customer_email,
                row.customer_phone,
                row.price_at_booking,
                row.discount_code,
                row.discount_amount,
                to_gbp(int(row.deposit_pence)) if row.deposit_pence is not None else None,
            ]
        writer.writerow([_safe(cell) for cell in cells])
    return buffer.getvalue()
