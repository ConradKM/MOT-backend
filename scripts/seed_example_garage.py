"""Seed a fully-populated example garage for manual/exploratory testing.

Creates one garage - "Kingsway MOT & Service Centre" - wired up to exercise
every feature the API and the staff frontend expose:

  * roles (the protected OWNER + seeded STAFF, plus custom ones) and
    multi-role employees
  * customers with every combination of contact details (both / phone only /
    email only / neither)
  * vehicles across the full MOT-status spectrum (expired, expiring soon,
    valid, unknown) and one customer with no vehicle at all
  * MOT record history (PASS and FAIL rows, notes, expiry back-fill)
  * garage appointment types in all three lifecycle states
    (ACTIVE / HIDDEN / DEPRECATED), with and without a base price / default
    duration
  * checklist templates (for MOT, Full Service and Brake Repair) whose items
    between them use every media_type and every "media required for status"
    trigger
  * appointments in every status (REQUESTED, BOOKED, IN_PROGRESS, COMPLETED,
    ACTION_NEEDED, CANCELLED, NO_SHOW), spread over several employees and
    days, including one with no vehicle attached
  * snapshotted per-appointment checklists with logged item results covering
    every checklist item status, plus checklist item media rows
  * reminders in PENDING and SENT states across the EMAIL and SMS channels

Idempotent: re-running deletes the existing example garage (only that one,
matched by name) and everything under it, then recreates it. Pass --fresh to
instead TRUNCATE every table first for a completely clean database.

Usage (from the repo root, with the dev DB reachable via DATABASE_URL):

    .venv/Scripts/python scripts/seed_example_garage.py
    .venv/Scripts/python scripts/seed_example_garage.py --fresh
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

# Allow `python scripts/seed_example_garage.py` from anywhere - put the repo
# root (this file's parent's parent) on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# The app's Config reads DATABASE_URL straight from the environment; the
# `flask` CLI would normally load .env for us, so do it here for the
# standalone script.
load_dotenv()

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.garages.slug import slugify_unique  # noqa: E402
from app.models.appointments.appointment import Appointment  # noqa: E402
from app.models.appointments.appointment_checklist import AppointmentChecklist  # noqa: E402
from app.models.appointments.appointment_checklist_item import (  # noqa: E402
    AppointmentChecklistItem,
)
from app.models.appointments.appointment_type import GarageAppointmentType  # noqa: E402
from app.models.appointments.checklist_item_media import ChecklistItemMedia  # noqa: E402
from app.models.appointments.checklist_template import ChecklistTemplate  # noqa: E402
from app.models.appointments.checklist_template_item import (  # noqa: E402
    CHECKLIST_ITEM_STATUSES,
    ChecklistTemplateItem,
)
from app.models.customer import Customer  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.garage import Garage  # noqa: E402
from app.models.mot_record import MOTRecord  # noqa: E402
from app.models.reminder import Reminder  # noqa: E402
from app.models.role import Role  # noqa: E402
from app.models.vehicle import Vehicle  # noqa: E402
from app.mot_records.routes import _sync_vehicle_mot_expiry  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

GARAGE_NAME = "Kingsway MOT & Service Centre"
PASSWORD = "Password123!"  # every seeded employee shares this

NOW = datetime.now(UTC)
TODAY = NOW.date()


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A tz-aware instant on the day `day_offset` days from today."""
    return datetime.combine(
        TODAY + timedelta(days=day_offset), time(hour, minute), tzinfo=UTC
    )


# Tables in FK-safe delete order (children first). Used both for the
# per-garage reset and, in --fresh mode, for a full wipe.
_DELETE_ORDER = [
    "checklist_item_media",
    "appointment_checklist_items",
    "appointment_checklists",
    "reminders",
    "appointments",
    "mot_records",
    "checklist_template_items",
    "checklist_templates",
    "garage_appointment_types",
    "vehicles",
    "customers",
    "employees",  # employee_roles rows cascade via FK ON DELETE CASCADE
    "roles",
    "garages",
]


def wipe_everything() -> None:
    db.session.execute(
        db.text(
            "TRUNCATE TABLE "
            + ", ".join(_DELETE_ORDER)
            + " RESTART IDENTITY CASCADE"
        )
    )
    db.session.commit()
    print("Truncated every table (--fresh).")


def delete_existing_example_garage() -> None:
    garage = Garage.query.filter_by(name=GARAGE_NAME).one_or_none()
    if garage is None:
        return

    gid = garage.id
    for table in _DELETE_ORDER:
        column = "id" if table == "garages" else "garage_id"
        db.session.execute(
            db.text(f"DELETE FROM {table} WHERE {column} = :gid"), {"gid": gid}
        )
    db.session.commit()
    print(f"Removed the previous '{GARAGE_NAME}' and everything under it.")


# --------------------------------------------------------------------------


def seed() -> None:
    # ---- Garage ---------------------------------------------------------
    garage = Garage(
        name=GARAGE_NAME,
        slug=slugify_unique(GARAGE_NAME, db.session),
        email="bookings@kingsway-mot.example",
        phone="+44 20 7946 0199",
        address="14-16 Kingsway Industrial Estate, London, EC1A 2BX",
        postcode="EC1A 2BX",
        website="https://kingsway-mot.example",
    )
    db.session.add(garage)
    db.session.flush()

    # ---- Roles --------------------------------------------------------
    # OWNER is the reserved/protected role; STAFF is the default seeded one.
    role_owner = Role(garage_id=garage.id, name="OWNER")
    role_staff = Role(garage_id=garage.id, name="STAFF")
    role_mechanic = Role(garage_id=garage.id, name="Mechanic")
    role_tester = Role(garage_id=garage.id, name="MOT Tester")
    role_advisor = Role(garage_id=garage.id, name="Service Advisor")
    role_reception = Role(garage_id=garage.id, name="Receptionist")
    db.session.add_all(
        [
            role_owner,
            role_staff,
            role_mechanic,
            role_tester,
            role_advisor,
            role_reception,
        ]
    )
    db.session.flush()

    # ---- Employees --------------------------------------------------
    def make_employee(email, first, last, roles):
        return Employee(
            garage_id=garage.id,
            email=email,
            first_name=first,
            last_name=last,
            password_hash=generate_password_hash(PASSWORD),
            roles=roles,
        )

    owner = make_employee(
        "owner@kingsway-mot.example", "Dawn", "Whitfield", [role_owner, role_tester]
    )
    greg = make_employee(
        "greg.mason@kingsway-mot.example", "Greg", "Mason", [role_mechanic]
    )
    tom = make_employee(
        "tom.baxter@kingsway-mot.example", "Tom", "Baxter", [role_tester, role_mechanic]
    )
    rachel = make_employee(
        "rachel.adnan@kingsway-mot.example",
        "Rachel",
        "Adnan",
        [role_advisor, role_reception],
    )
    # Deliberately left with no name and only the default STAFF role, to
    # exercise the "employee with blank first/last name" rendering path.
    sam = make_employee("sam.kaur@kingsway-mot.example", None, None, [role_staff])
    db.session.add_all([owner, greg, tom, rachel, sam])
    db.session.flush()

    # ---- Appointment types ------------------------------------------
    def make_type(name, description, price, duration, status="ACTIVE"):
        t = GarageAppointmentType(
            garage_id=garage.id,
            name=name,
            description=description,
            base_price=None if price is None else Decimal(price),
            default_duration_minutes=duration,
            status=status,
        )
        db.session.add(t)
        return t

    type_mot = make_type(
        "MOT Test", "Class 4 MOT test to current DVSA standards.", "54.85", 45
    )
    type_service = make_type(
        "Full Service",
        "Comprehensive 60-point service including oil and filter change.",
        "189.00",
        90,
    )
    type_mot_service = make_type(
        "MOT + Full Service",
        "Full service carried out alongside the MOT test, same visit.",
        "235.00",
        120,
    )
    type_brakes = make_type(
        "Brake Repair", "Diagnosis and replacement of brake components.", "120.00", 60
    )
    type_diagnostic = make_type(
        "Diagnostic Check",
        "Engine management / fault-code diagnostic investigation.",
        "60.00",
        30,
    )
    # No price and no default duration - still bookable, but an explicit
    # end_time is required.
    make_type("Courtesy Check", "Free visual safety check while you wait.", None, None)
    # Not offered for new bookings, but kept for reference / possible revival.
    make_type(
        "Tyre Fitting",
        "Supply and fit of customer-supplied tyres. Paused - see workshop.",
        None,
        30,
        status="HIDDEN",
    )
    # Retired for good.
    make_type(
        "Air-Con Regas (R134a)",
        "Discontinued - we no longer stock R134a refrigerant.",
        "75.00",
        45,
        status="DEPRECATED",
    )
    db.session.flush()

    # ---- Checklist templates --------------------------------------
    # Item tuple: (order, label, is_compulsory, media_type,
    #              media_required_for_statuses)
    # These three templates are genuinely automotive (DVSA-style grading), so
    # every item opts into the full CHECKLIST_ITEM_STATUSES preset rather
    # than the generic DONE/NOT_APPLICABLE default a brand-new item gets -
    # see app/models/appointments/checklist_template_item.py.
    def build_template(appointment_type, items, customer_visible_labels=()):
        template = ChecklistTemplate(
            garage_id=garage.id, appointment_type_id=appointment_type.id
        )
        db.session.add(template)
        db.session.flush()
        rows = []
        for order, label, compulsory, media_type, required_for in items:
            row = ChecklistTemplateItem(
                garage_id=garage.id,
                checklist_template_id=template.id,
                order=order,
                label=label,
                is_compulsory=compulsory,
                media_type=media_type,
                media_required_for_statuses=list(required_for),
                result_options=list(CHECKLIST_ITEM_STATUSES),
                visible_to_customer=label in customer_visible_labels,
            )
            db.session.add(row)
            rows.append(row)
        db.session.flush()
        return template, rows

    _, mot_items = build_template(
        type_mot,
        [
            (1, "Brakes - condition and operation", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
            (2, "Tyres and road wheels", True, "PHOTO", ["MINOR", "MAJOR", "DANGEROUS"]),
            (3, "Lights, indicators and reflectors", True, "NONE", []),
            (4, "Steering and suspension", True, "EITHER", ["MAJOR", "DANGEROUS"]),
            (5, "Windscreen, wipers and washers", False, "NONE", []),
            (6, "Exhaust, fuel and emissions", True, "VIDEO", ["MAJOR", "DANGEROUS"]),
            (7, "Seatbelts and restraint systems", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
            (8, "Body, structure and general items", False, "PHOTO", ["DANGEROUS"]),
            (9, "Registration plates and VIN", True, "NONE", []),
        ],
        # The customer-facing summary of a DVSA test - the full 9-point
        # checklist stays internal working detail (item 5's "don't blindly
        # expose every internal item").
        customer_visible_labels={
            "Brakes - condition and operation",
            "Tyres and road wheels",
            "Lights, indicators and reflectors",
            "Exhaust, fuel and emissions",
        },
    )

    _, service_items = build_template(
        type_service,
        [
            (1, "Engine oil and filter change", True, "NONE", []),
            (2, "Air filter inspection", False, "PHOTO", ["ADVISORY", "MINOR"]),
            (3, "Brake fluid level and condition", True, "NONE", []),
            (4, "Coolant level and antifreeze strength", True, "NONE", []),
            (5, "Battery health check", False, "PHOTO", ["ADVISORY"]),
            (6, "Auxiliary drive belt condition", False, "EITHER", ["MINOR", "MAJOR"]),
            (7, "Tyre tread depth and pressures", True, "PHOTO", ["MINOR", "MAJOR"]),
            (8, "Wiper blades and washer fluid", False, "NONE", []),
        ],
        customer_visible_labels={
            "Engine oil and filter change",
            "Brake fluid level and condition",
            "Coolant level and antifreeze strength",
            "Tyre tread depth and pressures",
        },
    )

    _, brake_items = build_template(
        type_brakes,
        [
            (1, "Brake pad thickness - all corners", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
            (2, "Brake disc condition and thickness", True, "PHOTO", ["MAJOR", "DANGEROUS"]),
            (3, "Brake hoses and pipes", True, "EITHER", ["MAJOR", "DANGEROUS"]),
            (4, "Handbrake travel and holding", True, "NONE", []),
            (5, "Brake fluid moisture content", False, "NONE", ["DANGEROUS"]),
            (6, "Road test - braking in a straight line", True, "VIDEO", ["MAJOR", "DANGEROUS"]),
        ],
        customer_visible_labels={
            "Brake pad thickness - all corners",
            "Brake disc condition and thickness",
            "Road test - braking in a straight line",
        },
    )
    # type_mot_service and type_diagnostic intentionally have no template,
    # so the "this appointment type has no checklist template yet" path is
    # reachable too.

    # ---- Customers ----------------------------------------------
    cust_oliver = Customer(
        garage_id=garage.id,
        first_name="Oliver",
        last_name="Bennett",
        email="oliver.bennett@example.com",
        phone="+44 7700 900101",
    )
    cust_priya = Customer(
        garage_id=garage.id,
        first_name="Priya",
        last_name="Sharma",
        email="priya.sharma@example.com",
        phone="+44 7700 900102",
    )
    # Phone only - no email on file.
    cust_marcus = Customer(
        garage_id=garage.id,
        first_name="Marcus",
        last_name="Johnson",
        email=None,
        phone="+44 7700 900103",
    )
    # Email only - no phone on file.
    cust_sofia = Customer(
        garage_id=garage.id,
        first_name="Sofia",
        last_name="Rossi",
        email="sofia.rossi@example.com",
        phone=None,
    )
    cust_liam = Customer(
        garage_id=garage.id,
        first_name="Liam",
        last_name="O'Connor",
        email="liam.oconnor@example.com",
        phone="+44 7700 900105",
    )
    # Neither email nor phone, and (below) no vehicle - the minimal customer.
    cust_grace = Customer(
        garage_id=garage.id,
        first_name="Grace",
        last_name="Chen",
        email=None,
        phone=None,
    )
    db.session.add_all(
        [cust_oliver, cust_priya, cust_marcus, cust_sofia, cust_liam, cust_grace]
    )
    db.session.flush()

    # ---- Vehicles ---------------------------------------------
    def make_vehicle(customer, reg, make, model, year, mileage):
        v = Vehicle(
            garage_id=garage.id,
            customer_id=customer.id,
            registration_number=reg,
            make=make,
            model=model,
            year=year,
            current_mileage=mileage,
        )
        db.session.add(v)
        return v

    veh_focus = make_vehicle(cust_oliver, "OB11 KWY", "Ford", "Focus", 2019, 48200)
    veh_audi = make_vehicle(cust_oliver, "OB08 AUD", "Audi", "A4", 2010, 132450)
    veh_golf = make_vehicle(cust_priya, "PS19 XYZ", "Volkswagen", "Golf", 2021, 22150)
    veh_bmw = make_vehicle(cust_marcus, "MJ64 TRD", "BMW", "320d", 2016, 91800)
    veh_transit = make_vehicle(cust_marcus, "MJ18 VAN", "Ford", "Transit", 2018, 74300)
    veh_fiat = make_vehicle(cust_sofia, "SR20 FIA", "Fiat", "500", 2020, 18900)
    veh_corolla = make_vehicle(cust_liam, "LO22 HYB", "Toyota", "Corolla", 2022, 12400)
    # cust_priya also gets a second, brand-new vehicle with no MOT history
    # yet (MOT status "unknown").
    veh_id3 = make_vehicle(cust_priya, "PS73 EVX", "Volkswagen", "ID.3", 2023, 6100)
    db.session.flush()

    # ---- MOT records ---------------------------------------
    def add_mot(vehicle, mot_date, expiry_date, result, notes=None):
        db.session.add(
            MOTRecord(
                garage_id=garage.id,
                vehicle_id=vehicle.id,
                mot_date=mot_date,
                expiry_date=expiry_date,
                result=result,
                notes=notes,
            )
        )

    # Uses the same PASS-only rule production does (app/mot_records/routes.py)
    # so a FAIL's placeholder expiry_date never masquerades as a fresh
    # certificate - see the notes on each FAIL row below.
    def sync_expiry(vehicle):
        _sync_vehicle_mot_expiry(vehicle)

    # Focus: 3 years of history, current certificate expiring in ~18 days
    # -> "expiring soon".
    add_mot(veh_focus, TODAY - timedelta(days=730), TODAY - timedelta(days=365),
            "PASS", "No advisories.")
    # A FAIL grants no new expiry - it's stored as its own mot_date (zero
    # forward validity), not the retest's eventual expiry.
    add_mot(veh_focus, TODAY - timedelta(days=365), TODAY - timedelta(days=365),
            "FAIL", "Failed on offside headlamp aim; rectified and re-tested "
            "18 days later.")
    add_mot(veh_focus, TODAY - timedelta(days=347), TODAY + timedelta(days=18),
            "PASS", "Advisory: nearside front tyre worn close to limit (2.5mm).")
    sync_expiry(veh_focus)

    # Audi: last (passed) certificate lapsed 3 weeks ago -> "expired". The
    # subsequent FAIL is a lapsed-MOT retest that didn't pass, so it must not
    # grant a new expiry either - sync_expiry correctly falls back to the
    # older PASS's real expiry, not the FAIL's placeholder date.
    add_mot(veh_audi, TODAY - timedelta(days=386), TODAY - timedelta(days=21),
            "PASS", "Advisory: light corrosion on rear subframe.")
    add_mot(veh_audi, TODAY - timedelta(days=5), TODAY - timedelta(days=5),
            "FAIL", "Excessive play in nearside front lower suspension arm "
            "ball joint (dangerous). MOT had already lapsed before this retest.")
    sync_expiry(veh_audi)

    # Golf: healthy, ~10 months left -> "valid".
    add_mot(veh_golf, TODAY - timedelta(days=60), TODAY + timedelta(days=305),
            "PASS", "No defects.")
    sync_expiry(veh_golf)

    # BMW: comfortably valid.
    add_mot(veh_bmw, TODAY - timedelta(days=120), TODAY + timedelta(days=245),
            "PASS", "Advisory: front brake discs worn, pitted or scored.")
    sync_expiry(veh_bmw)

    # Transit: due very soon (~6 days) -> "expiring soon".
    add_mot(veh_transit, TODAY - timedelta(days=359), TODAY + timedelta(days=6),
            "PASS", "Advisory: oil leak, not excessive.")
    sync_expiry(veh_transit)

    # Fiat: valid.
    add_mot(veh_fiat, TODAY - timedelta(days=90), TODAY + timedelta(days=275),
            "PASS")
    sync_expiry(veh_fiat)

    # Corolla: valid, clean.
    add_mot(veh_corolla, TODAY - timedelta(days=200), TODAY + timedelta(days=165),
            "PASS", "No advisories.")
    sync_expiry(veh_corolla)

    # veh_id3 and (customer Grace has none) -> mot_expiry_date stays NULL
    # -> "unknown".

    db.session.flush()

    # ---- Appointments -----------------------------------
    def make_appt(employee, customer, appt_type, start, end, status, vehicle=None,
                  notes=None):
        a = Appointment(
            garage_id=garage.id,
            employee_id=employee.id,
            customer_id=customer.id,
            vehicle_id=None if vehicle is None else vehicle.id,
            appointment_type_id=appt_type.id,
            start_time=start,
            end_time=end,
            status=status,
            notes=notes,
        )
        db.session.add(a)
        return a

    appt_completed_mot = make_appt(
        tom, cust_oliver, type_mot, at(-6, 9, 0), at(-6, 9, 45), "COMPLETED",
        vehicle=veh_focus, notes="Passed with one advisory. Certificate issued.",
    )
    appt_completed_service = make_appt(
        greg, cust_priya, type_service, at(-5, 10, 0), at(-5, 11, 30), "COMPLETED",
        vehicle=veh_golf, notes="Service completed. Air filter replaced.",
    )
    appt_action_needed = make_appt(
        greg, cust_marcus, type_brakes, at(-3, 14, 0), at(-3, 15, 0), "ACTION_NEEDED",
        vehicle=veh_bmw,
        notes="Dangerous front brakes found. Awaiting customer approval for "
        "pads + discs before road test.",
    )
    make_appt(
        tom, cust_sofia, type_diagnostic, at(-2, 11, 0), at(-2, 11, 30), "NO_SHOW",
        vehicle=veh_fiat, notes="Customer did not attend; left voicemail.",
    )
    make_appt(
        greg, cust_priya, type_mot_service, at(-1, 15, 0), at(-1, 17, 0), "CANCELLED",
        vehicle=veh_golf, notes="Customer rescheduled - see booking next week.",
    )
    appt_in_progress = make_appt(
        tom, cust_liam, type_mot, at(0, 8, 30), at(0, 9, 15), "IN_PROGRESS",
        vehicle=veh_corolla,
    )
    make_appt(
        greg, cust_oliver, type_service, at(0, 13, 0), at(0, 14, 30), "BOOKED",
        vehicle=veh_focus, notes="While-you-wait service.",
    )
    # BOOKED with no vehicle attached yet - customer will confirm which car.
    make_appt(
        tom, cust_grace, type_mot, at(1, 9, 0), at(1, 9, 45), "BOOKED",
        vehicle=None, notes="New customer - vehicle details to follow.",
    )
    make_appt(
        greg, cust_marcus, type_mot_service, at(3, 10, 0), at(3, 12, 0), "BOOKED",
        vehicle=veh_transit,
    )
    make_appt(
        rachel, cust_sofia, type_diagnostic, at(5, 9, 30), at(5, 10, 0), "REQUESTED",
        vehicle=veh_fiat, notes="Submitted via the online booking form - needs confirming.",
    )
    make_appt(
        greg, cust_liam, type_brakes, at(8, 16, 0), at(8, 17, 0), "BOOKED",
        vehicle=veh_corolla,
    )
    db.session.flush()

    # ---- Per-appointment checklists (snapshots) --------
    # Snapshot template rows onto an appointment, then apply logged results.
    # `results` maps template-item order -> (status, notes, [media_types]).
    def snapshot_checklist(appointment, template_rows, completed_by, completed_at,
                           results):
        checklist = AppointmentChecklist(
            garage_id=garage.id,
            appointment_id=appointment.id,
            checklist_template_id=template_rows[0].checklist_template_id,
        )
        db.session.add(checklist)
        db.session.flush()

        for trow in template_rows:
            status, notes, media_types = results.get(
                trow.order, ("NOT_CHECKED", None, [])
            )
            logged = status != "NOT_CHECKED"
            item = AppointmentChecklistItem(
                garage_id=garage.id,
                appointment_checklist_id=checklist.id,
                checklist_template_item_id=trow.id,
                order=trow.order,
                label=trow.label,
                description=trow.description,
                is_compulsory=trow.is_compulsory,
                media_type=trow.media_type,
                media_required_for_statuses=list(trow.media_required_for_statuses),
                result_options=list(trow.result_options),
                visible_to_customer=trow.visible_to_customer,
                status=status,
                notes=notes,
                completed_by_employee_id=completed_by.id if logged else None,
                completed_at=completed_at if logged else None,
            )
            db.session.add(item)
            db.session.flush()

            for media_type in media_types:
                db.session.add(
                    ChecklistItemMedia(
                        garage_id=garage.id,
                        appointment_checklist_item_id=item.id,
                        media_type=media_type,
                        # No upload endpoint yet - storage_key/uploaded_at
                        # stay NULL, as they would in real use today.
                        storage_key=None,
                        uploaded_at=None,
                    )
                )
        return checklist

    # Completed MOT: a clean pass with a single tyre advisory.
    snapshot_checklist(
        appt_completed_mot, mot_items, tom, at(-6, 9, 40),
        {
            1: ("PASS", "Pads 6mm front / 5mm rear. Discs within tolerance.", ["PHOTO"]),
            2: ("ADVISORY", "Nearside front tyre 2.5mm - advise replacement soon.", ["PHOTO"]),
            3: ("PASS", None, []),
            4: ("PASS", "No play detected.", []),
            5: ("PASS", "Small stone chip in swept area, within limits.", []),
            6: ("PASS", "Emissions within limits. Lambda 1.00.", []),
            7: ("PASS", None, []),
            8: ("PASS", None, []),
            9: ("PASS", "Plates and VIN legible and matching.", []),
        },
    )

    # Completed Full Service: mix of pass / rectified / advisory / recommended
    # / customer-declined.
    snapshot_checklist(
        appt_completed_service, service_items, greg, at(-5, 11, 20),
        {
            1: ("PASS", "5W-30 fully synthetic, 4.2L. Filter replaced.", []),
            2: ("RECTIFIED", "Filter blocked - replaced with new element.", ["PHOTO"]),
            3: ("PASS", "Fluid clear, level correct.", []),
            4: ("PASS", "Antifreeze protection to -37C.", []),
            5: ("ADVISORY", "11.9V rested, 420 CCA of 600 rated - monitor.", ["PHOTO"]),
            6: ("RECOMMENDED", "Fine surface cracking - recommend replacement next service.", []),
            7: ("PASS", "All tyres 4mm+ and set to placard pressures.", []),
            8: ("CUSTOMER_DECLINED", "Blades smearing; customer declined replacement.", []),
        },
    )

    # Brake Repair, still ACTION_NEEDED: dangerous + major findings logged,
    # road test deferred (left NOT_CHECKED).
    snapshot_checklist(
        appt_action_needed, brake_items, greg, at(-3, 14, 45),
        {
            1: ("DANGEROUS", "Offside front pads worn to backing plate - metal to metal.",
                ["PHOTO", "VIDEO"]),
            2: ("MAJOR", "Offside front disc scored, 2.1mm under minimum thickness.", ["PHOTO"]),
            3: ("PASS", "Hoses and pipes sound, no corrosion or chafing.", []),
            4: ("MINOR", "Excessive handbrake travel - adjust after pad replacement.", []),
            5: ("ADVISORY", "3.1% moisture in brake fluid - recommend fluid change.", []),
            # order 6 (road test) deliberately omitted -> NOT_CHECKED.
        },
    )

    # In-progress MOT: first checks done, one N/A, the rest not yet checked.
    snapshot_checklist(
        appt_in_progress, mot_items, tom, at(0, 8, 55),
        {
            1: ("PASS", "Brakes tested on rollers - balanced, efficiency OK.", []),
            2: ("PASS", "All tyres 4mm+, no damage.", []),
            3: ("MINOR", "Nearside rear number plate lamp not working.", ["PHOTO"]),
            5: ("NOT_APPLICABLE", "Heated windscreen element - not part of test.", []),
            # orders 4, 6, 7, 8, 9 omitted -> NOT_CHECKED.
        },
    )

    # ---- Reminders --------------------------------
    db.session.add_all(
        [
            Reminder(
                garage_id=garage.id,
                customer_id=cust_oliver.id,
                vehicle_id=veh_focus.id,
                type="MOT_DUE",
                channel="EMAIL",
                scheduled_at=NOW + timedelta(days=4),
                status="PENDING",
            ),
            Reminder(
                garage_id=garage.id,
                customer_id=cust_oliver.id,
                vehicle_id=veh_focus.id,
                type="MOT_DUE",
                channel="SMS",
                scheduled_at=NOW + timedelta(days=11),
                status="PENDING",
            ),
            Reminder(
                garage_id=garage.id,
                customer_id=cust_marcus.id,
                vehicle_id=veh_bmw.id,
                type="MOT_OVERDUE",
                channel="EMAIL",
                scheduled_at=NOW - timedelta(days=2),
                sent_at=NOW - timedelta(days=2),
                status="SENT",
            ),
            Reminder(
                garage_id=garage.id,
                customer_id=cust_priya.id,
                vehicle_id=veh_golf.id,
                appointment_id=appt_completed_service.id,
                type="SERVICE_FOLLOW_UP",
                channel="EMAIL",
                scheduled_at=NOW + timedelta(days=30),
                status="PENDING",
            ),
            Reminder(
                garage_id=garage.id,
                customer_id=cust_marcus.id,
                vehicle_id=veh_audi.id,
                type="MOT_OVERDUE",
                channel="SMS",
                scheduled_at=NOW - timedelta(days=10),
                sent_at=NOW - timedelta(days=10),
                status="SENT",
            ),
        ]
    )

    db.session.commit()

    # ---- Summary ------------------------------
    counts = {
        "roles": Role.query.filter_by(garage_id=garage.id).count(),
        "employees": Employee.query.filter_by(garage_id=garage.id).count(),
        "appointment types": GarageAppointmentType.query.filter_by(
            garage_id=garage.id
        ).count(),
        "checklist templates": ChecklistTemplate.query.filter_by(
            garage_id=garage.id
        ).count(),
        "customers": Customer.query.filter_by(garage_id=garage.id).count(),
        "vehicles": Vehicle.query.filter_by(garage_id=garage.id).count(),
        "MOT records": MOTRecord.query.filter_by(garage_id=garage.id).count(),
        "appointments": Appointment.query.filter_by(garage_id=garage.id).count(),
        "appointment checklists": AppointmentChecklist.query.filter_by(
            garage_id=garage.id
        ).count(),
        "checklist item media": ChecklistItemMedia.query.filter_by(
            garage_id=garage.id
        ).count(),
        "reminders": Reminder.query.filter_by(garage_id=garage.id).count(),
    }

    print()
    print(f"Seeded '{GARAGE_NAME}'  (garage id: {garage.id})")
    print("-" * 60)
    for label, n in counts.items():
        print(f"  {n:>3}  {label}")
    print("-" * 60)
    print("Log in on the staff frontend with any of:")
    print(f"    owner@kingsway-mot.example     / {PASSWORD}   (OWNER + MOT Tester)")
    print(f"    greg.mason@kingsway-mot.example / {PASSWORD}   (Mechanic)")
    print(f"    tom.baxter@kingsway-mot.example / {PASSWORD}   (MOT Tester + Mechanic)")
    print(f"    rachel.adnan@kingsway-mot.example / {PASSWORD} (Service Advisor + Receptionist)")
    print(f"    sam.kaur@kingsway-mot.example  / {PASSWORD}   (STAFF, no name set)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="TRUNCATE every table before seeding (full clean-slate reset).",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        print(f"Database: {app.config['SQLALCHEMY_DATABASE_URI']}")
        if args.fresh:
            wipe_everything()
        else:
            delete_existing_example_garage()
        seed()

    return 0


if __name__ == "__main__":
    sys.exit(main())
