"""API tests for the unauthenticated public booking endpoints.

GET  /api/public/<slug>
POST /api/public/<slug>/booking-requests
"""

import datetime

from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment import Appointment
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.vehicle import Vehicle

FUTURE_DATE = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()


def _valid_payload(**overrides):
    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex.turner@example.com",
        "customer_phone": "+44 7700 900123",
        "vehicle_registration": "PB11 REQ",
        "vehicle_make": "Ford",
        "vehicle_model": "Fiesta",
        "vehicle_year": 2017,
        "vehicle_mileage": 55000,
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
        "notes": "Rattle from the rear.",
    }
    payload.update(overrides)
    return payload


def _make_type(session, garage, name="MOT", status="ACTIVE"):
    t = GarageAppointmentType(garage_id=garage.id, name=name, status=status)
    session.add(t)
    session.commit()
    return t


# --------------------------------------------------------------------------
# GET /api/public/<slug>
# --------------------------------------------------------------------------


def test_get_public_garage_by_slug(client, garage):
    resp = client.get(f"/api/public/{garage.slug}")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == str(garage.id)
    assert body["name"] == garage.name
    assert body["slug"] == garage.slug
    assert body["appointment_types"] == []


def test_get_public_garage_lists_only_active_types(client, session, garage):
    _make_type(session, garage, name="MOT", status="ACTIVE")
    _make_type(session, garage, name="Old Service", status="HIDDEN")

    body = client.get(f"/api/public/{garage.slug}").get_json()

    names = {t["name"] for t in body["appointment_types"]}
    assert names == {"MOT"}


def test_get_public_garage_unknown_slug_returns_404(client):
    resp = client.get("/api/public/no-such-garage")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# POST /api/public/<slug>/booking-requests
# --------------------------------------------------------------------------


def test_submit_creates_a_pending_booking_request(client, session, garage):
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests", json=_valid_payload()
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "PENDING"

    row = db.session.get(BookingRequest, body["id"])
    assert row is not None
    assert row.garage_id == garage.id
    assert row.customer_email == "alex.turner@example.com"


def test_submit_does_not_create_live_records(client, session, garage):
    client.post(f"/api/public/{garage.slug}/booking-requests", json=_valid_payload())

    assert Customer.query.filter_by(garage_id=garage.id).count() == 0
    assert Vehicle.query.filter_by(garage_id=garage.id).count() == 0
    assert Appointment.query.filter_by(garage_id=garage.id).count() == 0


def test_submit_accepts_an_active_appointment_type(client, session, garage):
    appt_type = _make_type(session, garage)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_valid_payload(appointment_type_id=str(appt_type.id)),
    )

    assert resp.status_code == 201
    created = db.session.get(BookingRequest, resp.get_json()["id"])
    assert created.appointment_type_id == appt_type.id


def test_submit_strips_surrounding_whitespace(client, session, garage):
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_valid_payload(customer_first_name="  Alex  ", vehicle_registration=" pb11 req "),
    )

    row = db.session.get(BookingRequest, resp.get_json()["id"])
    assert row.customer_first_name == "Alex"
    assert row.vehicle_registration == "pb11 req"


def test_submit_unknown_slug_returns_404(client):
    resp = client.post(
        "/api/public/no-such-garage/booking-requests", json=_valid_payload()
    )
    assert resp.status_code == 404


def test_submit_missing_required_field_returns_422(client, garage):
    payload = _valid_payload()
    del payload["customer_email"]

    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=payload)
    assert resp.status_code == 422


def test_submit_past_date_returns_422(client, garage):
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_valid_payload(preferred_date=yesterday),
    )
    assert resp.status_code == 422


def test_submit_appointment_type_from_another_garage_returns_422(
    client, session, garage, second_garage
):
    other_type = _make_type(session, second_garage)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_valid_payload(appointment_type_id=str(other_type.id)),
    )
    assert resp.status_code == 422


def test_submit_inactive_appointment_type_returns_422(client, session, garage):
    hidden = _make_type(session, garage, name="Paused", status="HIDDEN")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_valid_payload(appointment_type_id=str(hidden.id)),
    )
    assert resp.status_code == 422


def test_submit_rejected_captcha_returns_400(client, garage, monkeypatch):
    monkeypatch.setattr(
        "app.public_booking.routes.verify_captcha", lambda token: False
    )

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests", json=_valid_payload()
    )
    assert resp.status_code == 400
    assert BookingRequest.query.count() == 0


# NOTE: end-to-end rate-limiting (429 after PUBLIC_BOOKING_RATELIMIT) is verified
# manually with curl (see the PR description). It isn't unit-tested here: the
# suite runs with RATELIMIT_ENABLED=False, and enabling it needs a second app
# instance, which would rebind the shared db / limiter extensions and break the
# rest of the suite.
