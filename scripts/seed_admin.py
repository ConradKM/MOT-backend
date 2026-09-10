#!/usr/bin/env python
"""Seed a persistent admin account + demo data into the DEV database.

    python scripts/seed_admin.py

Creates (idempotently - safe to re-run):
  - Garage "Admin Garage"
  - Employee admin@admin.com / admin (OWNER)
  - Two appointment types (MOT, Services), each with a checklist template
    covering the full range of item options (compulsory vs optional, every
    media_type, and both empty and populated media_required_for_statuses)
  - 10 customers + vehicles, with one appointment each spread across the
    current week (Mon-Fri, alternating MOT/Services)

WARNING - this account is intentionally insecure (a 5-character password
that would fail this app's own registration validation) and only exists
for local development convenience. It must never reach a real deployment.
Tracked for removal in issue #11 - delete this script and the
account/garage it creates when that issue is resolved.

This writes to the real dev database (the same one `flask run` uses), not
a throwaway test/demo database - the data is still there after the Flask
process restarts, because it lives in Postgres's own Docker volume, not in
the Flask process.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.checklist_template import ChecklistTemplate
from app.models.appointments.checklist_template_item import ChecklistTemplateItem
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.vehicle import Vehicle

ADMIN_EMAIL = "admin@admin.com"
ADMIN_PASSWORD = "admin"

MOT_CHECKLIST_ITEMS = [
    # (label, is_compulsory, media_type, media_required_for_statuses)
    ("Check headlights and indicators", True, "NONE", []),
    ("Check tyre tread depth and condition", True, "PHOTO", ["MINOR", "MAJOR", "DANGEROUS"]),
    ("Check front brake pad thickness", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
    ("Check exhaust emissions", True, "NONE", []),
    ("Check windscreen wipers and washers", False, "NONE", []),
    ("Check seatbelts", True, "VIDEO", ["DANGEROUS"]),
    ("Check horn operation", False, "EITHER", ["MAJOR"]),
]

SERVICE_CHECKLIST_ITEMS = [
    ("Change engine oil and filter", True, "NONE", []),
    ("Check coolant level", True, "NONE", []),
    ("Inspect brake fluid level", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
    ("Check battery condition", False, "NONE", []),
    ("Inspect air filter", False, "PHOTO", []),
    ("Road test", True, "NONE", []),
]

CUSTOMERS = [
    # (first, last, reg, make, model)
    ("Alice", "Cooper", "AB12CDE", "Ford", "Fiesta"),
    ("Bob", "Marley", "XY99ZZZ", "VW", "Golf"),
    ("Carol", "Danvers", "CD34EFG", "Toyota", "Corolla"),
    ("David", "Chen", "GH56IJK", "Honda", "Civic"),
    ("Eve", "Torres", "LM78NOP", "Nissan", "Micra"),
    ("Frank", "Ocean", "QR90STU", "BMW", "3 Series"),
    ("Grace", "Kelly", "VW12XYZ", "Audi", "A3"),
    ("Henry", "Ford", "AB34CDE", "Mercedes", "C-Class"),
    ("Isla", "Fisher", "EF56GHI", "Vauxhall", "Corsa"),
    ("Jack", "Sparrow", "JK78LMN", "Kia", "Sportage"),
]


def _monday_of_this_week(today: datetime) -> datetime:
    return today - timedelta(days=today.weekday())


def seed(app) -> None:
    with app.app_context():
        existing = Employee.query.filter_by(email=ADMIN_EMAIL).first()
        if existing:
            print(f"{ADMIN_EMAIL} already exists (garage_id={existing.garage_id}) - nothing to do.")
            return

        garage = Garage(name="Admin Garage")
        db.session.add(garage)
        db.session.flush()

        admin = Employee(
            garage_id=garage.id,
            email=ADMIN_EMAIL,
            password_hash=generate_password_hash(ADMIN_PASSWORD),
            role="OWNER",
        )
        db.session.add(admin)
        db.session.flush()

        appointment_types = {}
        type_specs = (("MOT", 45, MOT_CHECKLIST_ITEMS), ("Services", 90, SERVICE_CHECKLIST_ITEMS))
        for name, duration_minutes, items in type_specs:
            appointment_type = GarageAppointmentType(
                garage_id=garage.id,
                name=name,
                status="ACTIVE",
                default_duration_minutes=duration_minutes,
            )
            db.session.add(appointment_type)
            db.session.flush()

            template = ChecklistTemplate(
                garage_id=garage.id, appointment_type_id=appointment_type.id
            )
            db.session.add(template)
            db.session.flush()

            for order, (label, is_compulsory, media_type, media_required) in enumerate(items):
                db.session.add(
                    ChecklistTemplateItem(
                        garage_id=garage.id,
                        checklist_template_id=template.id,
                        order=order,
                        label=label,
                        is_compulsory=is_compulsory,
                        media_type=media_type,
                        media_required_for_statuses=media_required,
                    )
                )

            appointment_types[name] = appointment_type

        db.session.flush()

        monday = _monday_of_this_week(datetime.now(UTC)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # 2 appointments/day, Mon-Fri, alternating type, non-overlapping for
        # the single admin employee.
        slots = [(9, 0), (10, 30)]
        type_cycle = ["MOT", "Services"]

        for i, (first, last, reg, make, model) in enumerate(CUSTOMERS):
            customer = Customer(garage_id=garage.id, first_name=first, last_name=last)
            db.session.add(customer)
            db.session.flush()

            vehicle = Vehicle(
                garage_id=garage.id,
                customer_id=customer.id,
                registration_number=reg,
                make=make,
                model=model,
            )
            db.session.add(vehicle)
            db.session.flush()

            day_offset, slot_index = divmod(i, len(slots))
            hour, minute = slots[slot_index]
            start = monday + timedelta(days=day_offset, hours=hour, minutes=minute)
            end = start + timedelta(minutes=45)
            appt_type = appointment_types[type_cycle[i % len(type_cycle)]]

            db.session.add(
                Appointment(
                    garage_id=garage.id,
                    employee_id=admin.id,
                    customer_id=customer.id,
                    vehicle_id=vehicle.id,
                    appointment_type_id=appt_type.id,
                    start_time=start,
                    end_time=end,
                )
            )

        db.session.commit()

        print(f"Seeded '{garage.name}' (garage_id={garage.id})")
        print(f"  Admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print(f"  Appointment types: {', '.join(appointment_types)}")
        print(f"  {len(CUSTOMERS)} customers + vehicles + appointments, week of {monday.date()}")


if __name__ == "__main__":
    seed(create_app())
