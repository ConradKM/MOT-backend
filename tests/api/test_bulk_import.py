"""Bulk import of services and appointments from pasted JSON (app/bulk_import),
through the owner endpoints POST /api/appointment-types/bulk and
POST /api/appointments/bulk."""

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.appointment_reminders.defaults import seed_appointment_reminder_settings
from app.appointment_reminders.service import send_due_appointment_reminders
from app.bulk_import.appointments import TimeError, parse_wall_clock
from app.bulk_import.result import MAX_APPOINTMENTS, MAX_BODY_BYTES, MAX_SERVICES
from app.communications import events
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment_type_group import AppointmentTypeGroup
from app.models.customer import Customer
from app.models.employee import Employee
from tests.conftest import DEFAULT_PASSWORD, AuthenticatedClient

SERVICES_URL = "/api/appointment-types/bulk"
APPOINTMENTS_URL = "/api/appointments/bulk"

# 2030-01-07 is a Monday; the seeded schedule opens Mon-Fri 09:00-17:00.
MONDAY = "2030-01-07"


def _services(*rows):
    return {"services": list(rows)}


def _row(**overrides):
    row = {
        "service": "MOT",
        "start": f"{MONDAY}T10:00",
        "end": None,
        "status": "BOOKED",
        "employee_email": None,
        "customer": {
            "first_name": "Sam",
            "last_name": "Lee",
            "phone": "07123 456789",
            "email": None,
        },
        "notes": None,
    }
    row.update(overrides)
    return row


def _appointments(*rows):
    return {"appointments": list(rows)}


def _post(client, url, payload, *, dry_run=True, acknowledge_warnings=False):
    return client.post(
        url,
        json={"dry_run": dry_run, "acknowledge_warnings": acknowledge_warnings, "payload": payload},
    )


def _strip_volatile(body):
    """A result without ids minted for new rows, for preview/commit equality."""
    body = deepcopy(body)
    body.pop("dry_run")
    body.pop("committed")
    return body


@pytest.fixture()
def owner(authenticated_user):
    return authenticated_user


@pytest.fixture()
def diary(owner, garage_schedule, appointment_type):
    """An owner with the seeded schedule and a 60-minute 'MOT' service."""
    return owner


@pytest.fixture()
def event_spy(monkeypatch):
    """Every communications event emitted during the test."""
    seen = []
    monkeypatch.setattr(events, "_handlers", defaultdict(list))
    for event_type in events.EVENT_TYPES:
        events.register_handler(
            event_type, lambda event_type=event_type, **kw: seen.append(event_type)
        )
    return seen


@pytest.fixture()
def customer(customer, session):
    """Jane Doe with a real-format mobile, stored as staff typed it."""
    customer.phone = "07712 345678"
    session.commit()
    return customer


@pytest.fixture()
def staff_client(client, session, garage, staff_role):
    staff = Employee(
        garage_id=garage.id,
        email="staff@garage-a.example",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    return AuthenticatedClient(client, create_access_token(identity=str(staff.id)))


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------


def test_formats_describe_both_imports(owner):
    body = owner.client.get("/api/bulk-import/formats").get_json()

    for kind in ("services", "appointments"):
        assert set(body[kind]) == {"example", "json_schema", "ai_prompt_template"}
        assert kind in body[kind]["example"]
        assert "{{service_names}}" in body[kind]["ai_prompt_template"]
    assert "{{timezone}}" in body["appointments"]["ai_prompt_template"]
    assert "{{statuses}}" in body["appointments"]["ai_prompt_template"]
    assert (
        "vehicle"
        not in body["appointments"]["json_schema"]["properties"]["appointments"]["items"][
            "properties"
        ]
    )
    assert body["defaults"]["timezone"] == "Europe/London"
    assert "BOOKED" in body["defaults"]["statuses"]
    assert body["limits"] == {
        "max_body_bytes": MAX_BODY_BYTES,
        "max_services": MAX_SERVICES,
        "max_appointments": MAX_APPOINTMENTS,
    }


def test_the_garage_reports_its_timezone_for_the_prompt(owner):
    assert owner.client.get("/api/garage").get_json()["timezone"] == "UTC"


def test_the_example_imports_cleanly(owner, garage_schedule):
    """The example the UI shows must itself be a valid import."""
    formats = owner.client.get("/api/bulk-import/formats").get_json()
    body = _post(owner.client, SERVICES_URL, formats["services"]["example"]).get_json()
    assert body["ok"] and body["counts"]["error"] == 0


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERVICES_URL, APPOINTMENTS_URL])
def test_import_is_owner_only(staff_client, url):
    response = _post(staff_client, url, _services({"name": "X"}))
    assert response.status_code == 403


@pytest.mark.parametrize("url", [SERVICES_URL, APPOINTMENTS_URL])
def test_import_needs_a_login(client, url):
    assert client.post(url, json={"payload": {}}).status_code == 401


def test_import_lands_in_the_callers_business_only(
    owner, second_authenticated_client, second_garage, garage, session
):
    """The business comes from the JWT; nothing in the payload can aim it
    elsewhere."""
    payload = _services({"name": "Tyres", "garage_id": str(garage.id)})
    response = _post(
        second_authenticated_client, SERVICES_URL, payload, dry_run=False, acknowledge_warnings=True
    )

    assert response.status_code == 200
    names = {(t.garage_id, t.name) for t in session.query(GarageAppointmentType).all()}
    assert names == {(second_garage.id, "Tyres")}
    assert response.get_json()["items"][0]["warnings"][0]["path"] == "services[0].garage_id"


def test_another_businesss_services_are_not_matched(
    owner, second_authenticated_client, appointment_type
):
    """'MOT' exists in Garage A only - for Garage B it is new."""
    body = _post(second_authenticated_client, SERVICES_URL, _services({"name": "MOT"})).get_json()
    assert body["items"][0]["status"] == "NEW"


# --------------------------------------------------------------------------
# Payload shape and limits
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [[], {"service": []}, {"services": {"name": "x"}}, "services"],
)
def test_a_payload_of_the_wrong_shape_is_a_422(owner, payload):
    response = _post(owner.client, SERVICES_URL, payload)
    assert response.status_code == 422
    assert "services" in response.get_json()["message"]


def test_too_many_rows_is_a_422_naming_the_limit(owner):
    payload = _services(*[{"name": f"S{i}"} for i in range(MAX_SERVICES + 1)])
    response = _post(owner.client, SERVICES_URL, payload)
    assert response.status_code == 422
    assert str(MAX_SERVICES) in response.get_json()["message"]


def test_too_many_appointments_is_a_422_naming_the_limit(diary):
    payload = _appointments(*[_row() for _ in range(MAX_APPOINTMENTS + 1)])
    response = _post(diary.client, APPOINTMENTS_URL, payload)
    assert response.status_code == 422
    assert str(MAX_APPOINTMENTS) in response.get_json()["message"]


def test_an_oversized_body_is_refused_before_parsing(owner):
    payload = _services({"name": "x", "description": "a" * MAX_BODY_BYTES})
    response = _post(owner.client, SERVICES_URL, payload)
    assert response.status_code == 422
    assert "KB" in response.get_json()["message"]


def test_meta_and_notes_keys_are_ignored_silently_but_strays_warn(owner):
    payload = {"_meta": {"source": "ai"}, "notes": "x", "extra": 1, "services": []}
    body = _post(owner.client, SERVICES_URL, payload).get_json()
    assert body["warnings"] == [
        {"path": "extra", "message": "Not part of the import format; ignored."}
    ]


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------


def test_services_preview_writes_nothing(owner, session):
    body = _post(owner.client, SERVICES_URL, _services({"name": "Tyres"})).get_json()

    assert body["dry_run"] is True and body["committed"] is False
    assert body["counts"] == {"new": 1, "exists": 0, "error": 0, "warning": 0}
    assert session.query(GarageAppointmentType).count() == 0


def test_services_commit_creates_them_with_groups(owner, session, garage):
    payload = _services(
        {"name": "MOT Test", "base_price": "54.85", "default_duration_minutes": 45},
        {"name": "Full Service", "base_price": 189, "group": "Servicing"},
        {"name": "Interim Service", "group": "servicing"},
    )
    response = _post(owner.client, SERVICES_URL, payload, dry_run=False)

    assert response.status_code == 200
    body = response.get_json()
    assert body["committed"] is True
    assert body["items"][1]["normalised"]["base_price"] == "189"
    assert body["items"][1]["normalised"]["group_is_new"] is True

    groups = session.query(AppointmentTypeGroup).filter_by(garage_id=garage.id).all()
    assert [g.name for g in groups] == ["Servicing"]
    services = {s.name: s for s in session.query(GarageAppointmentType).all()}
    assert str(services["MOT Test"].base_price) == "54.85"
    assert services["Full Service"].group_id == services["Interim Service"].group_id
    assert (services["Full Service"].order, services["Interim Service"].order) == (0, 1)


def test_existing_names_are_skipped_and_reported(owner, appointment_type, session):
    payload = _services({"name": "  mot "}, {"name": "Tyres"})
    response = _post(owner.client, SERVICES_URL, payload, dry_run=False)
    body = response.get_json()

    assert [i["status"] for i in body["items"]] == ["EXISTS", "NEW"]
    assert body["items"][0]["normalised"]["existing"]["id"] == str(appointment_type.id)
    assert session.query(GarageAppointmentType).count() == 2


def test_rerunning_the_same_services_import_changes_nothing(owner, session):
    payload = _services({"name": "Tyres", "base_price": "10"}, {"name": "Brakes"})
    _post(owner.client, SERVICES_URL, payload, dry_run=False)
    payload["services"][0]["base_price"] = "999"

    body = _post(owner.client, SERVICES_URL, payload, dry_run=False).get_json()

    assert body["counts"]["exists"] == 2 and body["counts"]["new"] == 0
    assert (
        str(session.query(GarageAppointmentType).filter_by(name="Tyres").one().base_price)
        == "10.00"
    )


def test_existing_groups_are_reused(owner, session, garage):
    session.add(AppointmentTypeGroup(garage_id=garage.id, name="Tyres", order=0))
    session.commit()
    body = _post(
        owner.client, SERVICES_URL, _services({"name": "Puncture", "group": "TYRES"}), dry_run=False
    ).get_json()

    assert body["items"][0]["normalised"]["group_is_new"] is False
    assert session.query(AppointmentTypeGroup).count() == 1


@pytest.mark.parametrize(
    ("row", "path"),
    [
        ({"name": ""}, "services[0].name"),
        ({}, "services[0].name"),
        ({"name": "x" * 101}, "services[0].name"),
        ({"name": "A", "base_price": "£12"}, "services[0].base_price"),
        ({"name": "A", "base_price": "-1"}, "services[0].base_price"),
        ({"name": "A", "base_price": "1.005"}, "services[0].base_price"),
        ({"name": "A", "base_price": True}, "services[0].base_price"),
        ({"name": "A", "default_duration_minutes": 0}, "services[0].default_duration_minutes"),
        ({"name": "A", "default_duration_minutes": "45"}, "services[0].default_duration_minutes"),
        ({"name": "A", "default_duration_minutes": 1441}, "services[0].default_duration_minutes"),
        ({"name": "A", "status": "LIVE"}, "services[0].status"),
        ({"name": "A", "description": "d" * 501}, "services[0].description"),
        ({"name": "A", "group": ["x"]}, "services[0].group"),
        ("MOT", "services[0]"),
    ],
)
def test_service_errors_point_at_the_field(owner, row, path):
    body = _post(owner.client, SERVICES_URL, _services(row)).get_json()

    item = body["items"][0]
    assert item["status"] == "ERROR"
    assert body["ok"] is False
    assert path in [e["path"] for e in item["errors"]]


def test_every_error_in_a_row_is_reported_at_once(owner):
    row = {"name": "", "base_price": "x", "status": "NOPE"}
    errors = _post(owner.client, SERVICES_URL, _services(row)).get_json()["items"][0]["errors"]
    assert {e["path"] for e in errors} == {
        "services[0].name",
        "services[0].base_price",
        "services[0].status",
    }


def test_a_duplicate_name_within_the_paste_is_an_error(owner):
    body = _post(
        owner.client, SERVICES_URL, _services({"name": "Tyres"}, {"name": "tyres"})
    ).get_json()
    assert [i["status"] for i in body["items"]] == ["NEW", "ERROR"]
    assert "services[0]" in body["items"][1]["errors"][0]["message"]


def test_a_commit_with_errors_is_refused_and_writes_nothing(owner, session):
    response = _post(
        owner.client, SERVICES_URL, _services({"name": "Good"}, {"name": ""}), dry_run=False
    )

    assert response.status_code == 422
    assert response.get_json()["committed"] is False
    assert session.query(GarageAppointmentType).count() == 0


def test_a_commit_with_warnings_needs_acknowledgement(owner, session):
    payload = _services({"name": "Tyres", "colour": "red"})

    refused = _post(owner.client, SERVICES_URL, payload, dry_run=False)
    assert refused.status_code == 409
    assert session.query(GarageAppointmentType).count() == 0

    accepted = _post(owner.client, SERVICES_URL, payload, dry_run=False, acknowledge_warnings=True)
    assert accepted.status_code == 200
    assert session.query(GarageAppointmentType).count() == 1


def test_services_preview_matches_the_commit(owner, appointment_type):
    payload = _services({"name": "MOT"}, {"name": "Tyres", "group": "Wheels"}, {"name": "Brakes"})
    preview = _post(owner.client, SERVICES_URL, payload).get_json()
    commit = _post(owner.client, SERVICES_URL, payload, dry_run=False).get_json()
    assert _strip_volatile(preview) == _strip_volatile(commit)


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------


def test_appointment_preview_writes_nothing(diary, session):
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row())).get_json()

    assert body["counts"] == {"new": 1, "exists": 0, "error": 0, "warning": 0}
    item = body["items"][0]["normalised"]
    assert item["service"] == "MOT"
    assert item["start"] == "2030-01-07T10:00:00+00:00"
    assert item["end"] == "2030-01-07T11:00:00+00:00"
    assert item["customer"]["phone"] == "+447123456789"
    assert item["customer"]["match"] == "new"
    assert item["employee"]["email"] == diary.user.email
    assert body["context"] == {"timezone": "UTC"}
    assert session.query(Appointment).count() == 0
    assert session.query(Customer).count() == 0


def test_appointment_commit_mirrors_the_staff_create_path(
    diary, session, appointment_type, event_spy
):
    appointment_type.base_price = 54.85
    session.commit()
    response = _post(
        diary.client,
        APPOINTMENTS_URL,
        _appointments(_row(notes="Blue Ford Focus AB12 CDE")),
        dry_run=False,
    )

    assert response.status_code == 200, response.get_json()
    appointment = session.query(Appointment).one()
    assert appointment.employee_id == diary.user.id
    assert appointment.vehicle_id is None
    assert str(appointment.price_at_booking) == "54.85"
    assert appointment.appointment_type_name_at_booking == "MOT"
    assert appointment.end_time - appointment.start_time == timedelta(minutes=60)
    assert appointment.notes == "Blue Ford Focus AB12 CDE"
    assert appointment.imported_at is not None
    customer = appointment.customer
    assert (customer.first_name, customer.last_name, customer.phone) == (
        "Sam",
        "Lee",
        "+447123456789",
    )
    # No "booking confirmed" email/SMS, no conversation hooks.
    assert event_spy == []


def test_imported_appointments_get_no_automated_reminders_until_rescheduled(
    diary, session, garage, monkeypatch
):
    seed_appointment_reminder_settings(garage.id, session).enabled = True
    session.commit()
    sent = []
    monkeypatch.setattr(
        "app.appointment_reminders.service.record_and_send",
        lambda **kw: sent.append(kw["appointment"].id) or kw["appointment"],
    )
    _post(diary.client, APPOINTMENTS_URL, _appointments(_row()), dry_run=False)
    appointment = session.query(Appointment).one()
    due = appointment.start_time - timedelta(hours=1)

    send_due_appointment_reminders(now=due)
    assert sent == []

    moved = (appointment.start_time + timedelta(hours=1)).isoformat()
    response = diary.client.patch(
        f"/api/appointments/{appointment.id}",
        json={
            "start_time": moved,
            "end_time": (appointment.end_time + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.get_json()["imported_at"] is None

    send_due_appointment_reminders(now=due + timedelta(hours=1))
    assert set(sent) == {appointment.id}


def test_a_status_only_edit_keeps_an_appointment_imported(diary, session):
    _post(diary.client, APPOINTMENTS_URL, _appointments(_row()), dry_run=False)
    appointment = session.query(Appointment).one()
    response = diary.client.patch(
        f"/api/appointments/{appointment.id}", json={"status": "IN_PROGRESS"}
    )
    assert response.get_json()["imported_at"] is not None


def test_appointment_preview_matches_the_commit(diary, customer):
    payload = _appointments(
        _row(),
        _row(start=f"{MONDAY}T10:30"),  # clashes with the first
        _row(customer={"first_name": "Jane", "last_name": "Doe", "phone": "07712 345678"}),
        _row(start=f"{MONDAY}T20:00"),  # out of hours
        _row(start="2020-01-06T10:00"),  # in the past
        _row(),  # same as the first
    )
    preview = _post(diary.client, APPOINTMENTS_URL, payload).get_json()
    commit = _post(
        diary.client, APPOINTMENTS_URL, payload, dry_run=False, acknowledge_warnings=True
    ).get_json()

    assert commit["committed"] is True
    assert _strip_volatile(preview) == _strip_volatile(commit)


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "path"),
    [
        ({"service": "Full Service"}, "appointments[0].service"),
        ({"service": None}, "appointments[0].service"),
        ({"start": "next tuesday"}, "appointments[0].start"),
        ({"start": MONDAY}, "appointments[0].start"),
        ({"start": None}, "appointments[0].start"),
        ({"end": f"{MONDAY}T09:00"}, "appointments[0].end"),
        ({"end": f"{MONDAY}T10:00"}, "appointments[0].end"),
        ({"end": "soon"}, "appointments[0].end"),
        ({"status": "MAYBE"}, "appointments[0].status"),
        ({"employee_email": "nobody@example.com"}, "appointments[0].employee_email"),
        ({"customer": None}, "appointments[0].customer"),
        ({"customer": {"first_name": "Sam"}}, "appointments[0].customer.last_name"),
        ({"customer": {"last_name": "Lee"}}, "appointments[0].customer.first_name"),
        (
            {"customer": {"first_name": "S", "last_name": "L", "phone": "12345"}},
            "appointments[0].customer.phone",
        ),
        (
            {"customer": {"first_name": "S", "last_name": "L", "email": "not-an-email"}},
            "appointments[0].customer.email",
        ),
        (
            {"customer": {"first_name": "S" * 101, "last_name": "L"}},
            "appointments[0].customer.first_name",
        ),
        ({"notes": "n" * 2001}, "appointments[0].notes"),
    ],
)
def test_appointment_errors_point_at_the_field(diary, overrides, path):
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row(**overrides))).get_json()

    item = body["items"][0]
    assert item["status"] == "ERROR", item
    assert path in [e["path"] for e in item["errors"]]


def test_an_unknown_service_suggests_the_closest_names(diary):
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row(service="MOT test"))).get_json()
    assert "'MOT'" in body["items"][0]["errors"][0]["message"]


def test_a_service_with_no_duration_needs_an_end(diary, session, appointment_type):
    appointment_type.default_duration_minutes = None
    session.commit()
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row())).get_json()
    assert body["items"][0]["errors"][0]["path"] == "appointments[0].end"

    body = _post(
        diary.client, APPOINTMENTS_URL, _appointments(_row(end=f"{MONDAY}T10:20"))
    ).get_json()
    assert body["items"][0]["status"] == "NEW"


def test_an_inactive_service_cannot_be_imported(diary, session, appointment_type):
    appointment_type.status = "HIDDEN"
    session.commit()
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row())).get_json()
    assert "not active" in body["items"][0]["errors"][0]["message"]


def test_a_deactivated_employee_cannot_be_assigned(diary, session, garage, staff_role):
    session.add(
        Employee(
            garage_id=garage.id,
            email="gone@garage-a.example",
            password_hash="x",
            is_active=False,
            roles=[staff_role],
        )
    )
    session.commit()
    body = _post(
        diary.client, APPOINTMENTS_URL, _appointments(_row(employee_email="GONE@garage-a.example"))
    ).get_json()
    assert "deactivated" in body["items"][0]["errors"][0]["message"]


def test_another_businesss_employee_cannot_be_assigned(diary, second_user):
    body = _post(
        diary.client, APPOINTMENTS_URL, _appointments(_row(employee_email=second_user.email))
    ).get_json()
    assert body["items"][0]["errors"][0]["path"] == "appointments[0].employee_email"


def test_a_late_error_rolls_back_every_row(diary, session):
    payload = _appointments(*[_row(start=f"{MONDAY}T1{i}:00") for i in range(5)], _row(service="?"))
    response = _post(diary.client, APPOINTMENTS_URL, payload, dry_run=False)

    assert response.status_code == 422
    assert response.get_json()["counts"]["new"] == 5
    assert session.query(Appointment).count() == 0
    assert session.query(Customer).count() == 0


# --- statuses and employees ---------------------------------------------------


def test_status_defaults_to_booked_and_accepts_the_businesss_statuses(diary, seeded_statuses):
    body = _post(
        diary.client,
        APPOINTMENTS_URL,
        _appointments(_row(status=None), _row(start=f"{MONDAY}T12:00", status="completed")),
    ).get_json()
    assert [i["normalised"]["status"] for i in body["items"]] == ["BOOKED", "COMPLETED"]


def test_a_named_employee_is_assigned(diary, session, garage, staff_role):
    staff = Employee(
        garage_id=garage.id,
        email="tech@garage-a.example",
        first_name="Tess",
        last_name="Tech",
        password_hash="x",
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    _post(
        diary.client,
        APPOINTMENTS_URL,
        _appointments(_row(employee_email="Tech@Garage-A.example")),
        dry_run=False,
    )
    assert session.query(Appointment).one().employee_id == staff.id


# --- warnings -----------------------------------------------------------------


def test_a_double_booking_is_a_warning_not_an_error(diary, session):
    payload = _appointments(_row(), _row(start=f"{MONDAY}T10:30"))
    body = _post(diary.client, APPOINTMENTS_URL, payload).get_json()

    second = body["items"][1]
    assert second["status"] == "NEW"
    assert {"clash", "over_capacity"} <= set(second["normalised"]["flags"])
    assert body["ok"] is True and body["counts"]["warning"] == 1

    assert _post(diary.client, APPOINTMENTS_URL, payload, dry_run=False).status_code == 409
    assert session.query(Appointment).count() == 0
    response = _post(
        diary.client, APPOINTMENTS_URL, payload, dry_run=False, acknowledge_warnings=True
    )
    assert response.status_code == 200
    assert session.query(Appointment).count() == 2


def test_a_clash_with_an_existing_appointment_is_flagged(diary, make_appointment):
    make_appointment(datetime(2030, 1, 7, 10, 30, tzinfo=UTC))
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row())).get_json()
    assert "clash" in body["items"][0]["normalised"]["flags"]


def test_a_cancelled_row_never_clashes(diary):
    other = {"first_name": "Alex", "last_name": "Kim", "phone": "07712 345679"}
    payload = _appointments(_row(), _row(status="CANCELLED", customer=other))
    body = _post(diary.client, APPOINTMENTS_URL, payload).get_json()
    assert body["items"][1]["warnings"] == []


@pytest.mark.parametrize(
    ("start", "message"),
    [(f"{MONDAY}T07:00", "opening hours"), ("2030-01-12T10:00", "closed on this day")],
)
def test_out_of_hours_is_a_warning(diary, start, message):
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(_row(start=start))).get_json()
    item = body["items"][0]
    assert item["status"] == "NEW"
    assert "out_of_hours" in item["normalised"]["flags"]
    assert message in item["warnings"][0]["message"]


def test_past_dates_are_allowed_and_flagged(diary):
    body = _post(
        diary.client, APPOINTMENTS_URL, _appointments(_row(start="2021-03-01T10:00"))
    ).get_json()
    item = body["items"][0]
    assert item["status"] == "NEW"
    assert "past" in item["normalised"]["flags"]
    assert item["warnings"] == []


def test_a_vehicle_key_is_ignored_with_a_warning(diary, session):
    row = _row(vehicle={"registration": "AB12CDE"}, add_ons=["Wash"])
    body = _post(diary.client, APPOINTMENTS_URL, _appointments(row)).get_json()
    paths = [w["path"] for w in body["items"][0]["warnings"]]
    assert paths == ["appointments[0].add_ons", "appointments[0].vehicle"]

    _post(
        diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False, acknowledge_warnings=True
    )
    assert session.query(Appointment).one().vehicle_id is None


def test_a_landline_is_kept_with_a_warning(diary):
    row = _row(customer={"first_name": "S", "last_name": "L", "phone": "020 7946 0018"})
    item = _post(diary.client, APPOINTMENTS_URL, _appointments(row)).get_json()["items"][0]
    assert item["normalised"]["customer"]["phone"] == "+442079460018"
    assert "landline" in item["warnings"][0]["message"]


# --- customers ----------------------------------------------------------------


def test_customers_link_by_phone(diary, customer, session):
    row = _row(customer={"first_name": "J", "last_name": "D", "phone": "07712345678"})
    item = _post(diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False).get_json()[
        "items"
    ][0]
    assert item["normalised"]["customer"]["match"] == "existing"
    assert item["normalised"]["customer"]["existing_id"] == str(customer.id)
    assert session.query(Customer).count() == 1


def test_customers_link_by_email_when_there_is_no_phone(diary, customer, session):
    row = _row(customer={"first_name": "J", "last_name": "D", "email": "JANE.DOE@example.com"})
    _post(diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False)
    assert session.query(Appointment).one().customer_id == customer.id


def test_customers_are_never_linked_by_name_alone(diary, customer, session):
    row = _row(customer={"first_name": "Jane", "last_name": "Doe", "phone": None})
    _post(diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False)
    assert session.query(Customer).count() == 2


def test_an_archived_customer_is_reactivated(diary, customer, session):
    customer.is_active = False
    session.commit()
    row = _row(customer={"first_name": "J", "last_name": "D", "phone": "07712345678"})
    _post(diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False)
    session.refresh(customer)
    assert customer.is_active is True


def test_one_new_customer_across_rows_of_the_same_paste(diary, session):
    payload = _appointments(_row(), _row(start=f"{MONDAY}T14:00"))
    body = _post(diary.client, APPOINTMENTS_URL, payload, dry_run=False).get_json()

    assert body["items"][1]["normalised"]["customer"] == {
        **body["items"][1]["normalised"]["customer"],
        "match": "earlier_row",
        "same_as_index": 0,
    }
    assert session.query(Customer).count() == 1


def test_another_businesss_customer_is_never_linked(diary, second_customer, session):
    second_customer.phone = "07712 345670"
    session.commit()
    row = _row(customer={"first_name": "J", "last_name": "D", "phone": second_customer.phone})
    _post(diary.client, APPOINTMENTS_URL, _appointments(row), dry_run=False)
    assert session.query(Appointment).one().customer_id != second_customer.id


# --- re-running -----------------------------------------------------------------


@pytest.mark.parametrize(
    "customer_data",
    [
        {"first_name": "Sam", "last_name": "Lee", "phone": "07123 456789"},
        {"first_name": "Sam", "last_name": "Lee", "email": "sam@example.com"},
        {"first_name": "Sam", "last_name": "Lee"},
    ],
    ids=["phone", "email", "name-only"],
)
def test_rerunning_an_appointment_import_skips_everything(diary, session, customer_data):
    payload = _appointments(
        _row(customer=customer_data), _row(start=f"{MONDAY}T14:00", customer=customer_data)
    )
    first = _post(diary.client, APPOINTMENTS_URL, payload, dry_run=False)
    assert first.status_code == 200
    customers = session.query(Customer).count()
    # Rows with no phone or email are never linked by name, so each is a new
    # customer; with contact details they are one person.
    assert customers == (2 if set(customer_data) == {"first_name", "last_name"} else 1)

    again = _post(diary.client, APPOINTMENTS_URL, payload, dry_run=False).get_json()

    assert again["counts"] == {"new": 0, "exists": 2, "error": 0, "warning": 0}
    assert all(i["normalised"]["existing_id"] for i in again["items"])
    assert session.query(Appointment).count() == 2
    assert session.query(Customer).count() == customers


def test_a_duplicate_row_within_the_paste_is_skipped(diary, session):
    body = _post(
        diary.client,
        APPOINTMENTS_URL,
        _appointments(_row(), _row()),
        dry_run=False,
        acknowledge_warnings=True,
    ).get_json()

    assert [i["status"] for i in body["items"]] == ["NEW", "EXISTS"]
    assert body["items"][1]["normalised"]["duplicate_of"] == 0
    assert session.query(Appointment).count() == 1


def test_the_same_slot_for_a_different_customer_is_not_a_duplicate(diary):
    other = {"first_name": "Alex", "last_name": "Kim", "phone": "07712 345679"}
    body = _post(
        diary.client, APPOINTMENTS_URL, _appointments(_row(), _row(customer=other))
    ).get_json()
    assert [i["status"] for i in body["items"]] == ["NEW", "NEW"]


# --------------------------------------------------------------------------
# Time zones
# --------------------------------------------------------------------------

LONDON = ZoneInfo("Europe/London")


def test_wall_clock_times_are_the_businesss_local_time():
    assert parse_wall_clock("2030-07-01T09:30", LONDON) == datetime(2030, 7, 1, 8, 30, tzinfo=UTC)
    assert parse_wall_clock("2030-01-07T09:30", LONDON) == datetime(2030, 1, 7, 9, 30, tzinfo=UTC)


def test_an_explicit_offset_is_respected():
    assert parse_wall_clock("2030-07-01T09:30+02:00", LONDON) == datetime(
        2030, 7, 1, 7, 30, tzinfo=UTC
    )
    assert parse_wall_clock("2030-07-01T09:30Z", LONDON) == datetime(2030, 7, 1, 9, 30, tzinfo=UTC)


def test_a_time_skipped_by_the_clocks_going_forward_is_refused():
    with pytest.raises(TimeError, match="does not exist"):
        parse_wall_clock("2030-03-31T01:30", LONDON)


def test_a_time_repeated_by_the_clocks_going_back_is_refused():
    with pytest.raises(TimeError, match="happens twice"):
        parse_wall_clock("2030-10-27T01:30", LONDON)
    # ...unless the offset says which one.
    assert parse_wall_clock("2030-10-27T01:30+00:00", LONDON) == datetime(
        2030, 10, 27, 1, 30, tzinfo=UTC
    )


def test_the_business_timezone_drives_the_import(diary, garage, session):
    garage.timezone = "Europe/London"
    session.commit()
    body = _post(
        diary.client,
        APPOINTMENTS_URL,
        _appointments(_row(start="2030-07-01T10:00"), _row(start="2030-03-31T01:30")),
    ).get_json()

    assert body["context"] == {"timezone": "Europe/London"}
    assert body["items"][0]["normalised"]["start"] == "2030-07-01T10:00:00+01:00"
    assert "out_of_hours" not in body["items"][0]["normalised"]["flags"]
    assert body["items"][1]["errors"][0]["path"] == "appointments[1].start"
