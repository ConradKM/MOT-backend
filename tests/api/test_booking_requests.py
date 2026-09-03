"""API tests for the staff-facing booking-request review endpoints.

GET  /api/booking-requests/
GET  /api/booking-requests/<id>
POST /api/booking-requests/<id>/approve
POST /api/booking-requests/<id>/reject
"""

import datetime

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.vehicle import Vehicle

START = "2026-11-03T09:00:00+00:00"


def _make_type(session, garage, name="MOT", minutes=None, status="ACTIVE"):
    t = GarageAppointmentType(
        garage_id=garage.id, name=name, status=status, default_duration_minutes=minutes
    )
    session.add(t)
    session.commit()
    return t


def _pending_request(session, garage, **overrides):
    fields = dict(
        garage_id=garage.id,
        status="PENDING",
        customer_first_name="Pat",
        customer_last_name="Rivera",
        customer_email="pat.rivera@example.com",
        vehicle_registration="BR11 REQ",
        preferred_date=datetime.date.today() + datetime.timedelta(days=5),
        preferred_time=datetime.time(9, 0),
    )
    fields.update(overrides)
    br = BookingRequest(**fields)
    session.add(br)
    session.commit()
    return br


# --------------------------------------------------------------------------
# List / get
# --------------------------------------------------------------------------


def test_list_returns_the_garages_requests(authenticated_user, booking_request):
    resp = authenticated_user.client.get("/api/booking-requests/")

    assert resp.status_code == 200
    ids = [r["id"] for r in resp.get_json()]
    assert str(booking_request.id) in ids


def test_list_is_garage_scoped(
    authenticated_user, booking_request, session, second_garage
):
    _pending_request(session, second_garage, customer_email="other@example.com")

    body = authenticated_user.client.get("/api/booking-requests/").get_json()

    assert {r["id"] for r in body} == {str(booking_request.id)}


def test_list_filters_by_status(authenticated_user, booking_request):
    pending = authenticated_user.client.get(
        "/api/booking-requests/?status=PENDING"
    ).get_json()
    approved = authenticated_user.client.get(
        "/api/booking-requests/?status=APPROVED"
    ).get_json()

    assert str(booking_request.id) in {r["id"] for r in pending}
    assert approved == []


def test_get_one_request(authenticated_user, booking_request):
    resp = authenticated_user.client.get(f"/api/booking-requests/{booking_request.id}")

    assert resp.status_code == 200
    assert resp.get_json()["customer_email"] == booking_request.customer_email


def test_get_cross_garage_request_is_404(second_authenticated_client, booking_request):
    resp = second_authenticated_client.get(
        f"/api/booking-requests/{booking_request.id}"
    )
    assert resp.status_code == 404


def test_get_requires_authentication(client, booking_request):
    resp = client.get(f"/api/booking-requests/{booking_request.id}")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Approve
# --------------------------------------------------------------------------


def test_approve_creates_and_links_customer_vehicle_appointment(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "APPROVED"
    assert body["reviewed_by_employee_id"] == str(authenticated_user.user.id)

    customer = db.session.get(Customer, body["customer_id"])
    vehicle = db.session.get(Vehicle, body["vehicle_id"])
    appointment = db.session.get(Appointment, body["appointment_id"])
    assert customer.email == booking_request.customer_email
    assert vehicle.customer_id == customer.id
    assert appointment.customer_id == customer.id
    assert appointment.vehicle_id == vehicle.id
    assert appointment.status == "BOOKED"


def test_approve_reuses_an_existing_customer_matched_by_email(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)
    existing = Customer(
        garage_id=garage.id,
        first_name="Pat",
        last_name="Rivera",
        email="PAT.RIVERA@example.com",
    )
    session.add(existing)
    session.commit()

    authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )

    session.refresh(booking_request)
    assert booking_request.customer_id == existing.id
    assert Customer.query.filter_by(garage_id=garage.id).count() == 1


def test_approve_derives_end_time_from_the_type_default_duration(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage, minutes=60)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
        },
    )

    appointment = db.session.get(Appointment, resp.get_json()["appointment_id"])
    assert (appointment.end_time - appointment.start_time).total_seconds() == 3600


def test_approve_requires_an_employee_id(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={"appointment_type_id": str(appt_type.id), "start_time": START},
    )
    assert resp.status_code == 422


def test_approve_requires_a_resolvable_start_time(
    authenticated_user, session, garage
):
    appt_type = _make_type(session, garage)
    request_without_time = _pending_request(session, garage, preferred_time=None)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{request_without_time.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
        },
    )
    assert resp.status_code == 422


def test_approve_requires_an_appointment_type(
    authenticated_user, booking_request
):
    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={"employee_id": str(authenticated_user.user.id), "start_time": START},
    )
    assert resp.status_code == 422


def test_approve_rejects_an_employee_from_another_garage(
    authenticated_user, second_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(second_user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
        },
    )
    assert resp.status_code == 422


def test_approving_twice_conflicts(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)
    body = {
        "employee_id": str(authenticated_user.user.id),
        "appointment_type_id": str(appt_type.id),
        "start_time": START,
        "end_time": "2026-11-03T09:45:00+00:00",
    }
    authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve", json=body
    )
    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve", json=body
    )
    assert resp.status_code == 409


def test_approve_cross_garage_is_404(
    second_authenticated_client, booking_request
):
    resp = second_authenticated_client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={"start_time": START},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Reject
# --------------------------------------------------------------------------


def test_reject_sets_status_and_records_the_decision(
    authenticated_user, booking_request
):
    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/reject",
        json={"staff_notes": "No availability that week."},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "REJECTED"
    assert body["staff_notes"] == "No availability that week."
    assert body["reviewed_by_employee_id"] == str(authenticated_user.user.id)
    assert body["reviewed_at"] is not None


def test_reject_after_approve_conflicts(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage)
    authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/reject", json={}
    )
    assert resp.status_code == 409
