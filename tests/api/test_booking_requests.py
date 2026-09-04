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


def _make_type(session, garage, name="MOT", minutes=None, status="ACTIVE", base_price=None):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name=name,
        status=status,
        default_duration_minutes=minutes,
        base_price=base_price,
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


def test_approve_snapshots_the_types_price_onto_the_appointment(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage, base_price="54.85")

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )

    appointment = db.session.get(Appointment, resp.get_json()["appointment_id"])
    assert str(appointment.price_at_booking) == "54.85"


def test_approve_creates_the_appointments_checklist_instance(
    authenticated_user, session, garage, booking_request
):
    """Item 19 step 24: approval must immediately give the new appointment
    its checklist, not wait for staff to happen to open it."""
    appt_type = _make_type(session, garage)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type.id}/checklist-template")
    authenticated_user.client.post(
        f"/api/appointment-types/{appt_type.id}/checklist-template/items",
        json={"label": "Confirm arrival"},
    )

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )
    appointment_id = resp.get_json()["appointment_id"]

    checklist = authenticated_user.client.get(
        f"/api/appointments/{appointment_id}/checklist"
    )
    assert checklist.status_code == 200
    assert len(checklist.get_json()["items"]) == 1


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


# --------------------------------------------------------------------------
# Review-screen enrichment (customer/vehicle/service/duration/price, slot check)
# --------------------------------------------------------------------------


def test_list_row_includes_service_duration_and_price(
    authenticated_user, session, garage, booking_request
):
    appt_type = _make_type(session, garage, minutes=45)
    appt_type.base_price = "54.85"
    booking_request.appointment_type_id = appt_type.id
    session.commit()

    (row,) = authenticated_user.client.get("/api/booking-requests/").get_json()
    assert row["appointment_type"]["name"] == appt_type.name
    assert row["appointment_type"]["base_price"] == "54.85"
    assert row["duration_minutes"] == 45
    assert row["customer_full_name"] == "Pat Rivera"


def test_row_falls_back_to_the_garages_default_duration_with_no_type(
    authenticated_user, garage_schedule, booking_request
):
    (row,) = authenticated_user.client.get("/api/booking-requests/").get_json()
    assert row["appointment_type"] is None
    assert row["duration_minutes"] == garage_schedule.default_appointment_minutes


def test_slot_check_reports_available_when_capacity_remains(
    authenticated_user, garage_schedule, booking_request
):
    (row,) = authenticated_user.client.get("/api/booking-requests/").get_json()
    assert row["slot_check"]["checked"] is True
    assert row["slot_check"]["available"] is True


def test_slot_check_uses_the_requests_own_type_duration_not_the_garage_default(
    authenticated_user, session, garage, garage_schedule
):
    """Regression for item 15: the live re-check must use the *selected
    type's* duration. A 90-minute request starting 10:00 reserves through
    11:30 - a conflicting appointment at 11:00-11:30 falls outside the
    garage's flat 60-minute default window but must still be caught."""
    garage_schedule.capacity_per_slot = 1
    session.commit()
    long_type = _make_type(session, garage, minutes=90)
    other_type = _make_type(session, garage, name="Quick check")
    request = _pending_request(
        session, garage, appointment_type_id=long_type.id, preferred_time=datetime.time(10, 0)
    )

    other_customer = Customer(garage_id=garage.id, first_name="Jo", last_name="Public")
    session.add(other_customer)
    session.flush()
    session.add(
        Appointment(
            garage_id=garage.id,
            employee_id=authenticated_user.user.id,
            customer_id=other_customer.id,
            appointment_type_id=other_type.id,
            start_time=datetime.datetime.combine(
                request.preferred_date, datetime.time(11, 0), tzinfo=datetime.UTC
            ),
            end_time=datetime.datetime.combine(
                request.preferred_date, datetime.time(11, 30), tzinfo=datetime.UTC
            ),
            status="BOOKED",
        )
    )
    session.commit()

    row = next(
        r
        for r in authenticated_user.client.get("/api/booking-requests/").get_json()
        if r["id"] == str(request.id)
    )
    assert row["slot_check"]["checked"] is True
    assert row["slot_check"]["available"] is False


def test_slot_check_is_not_computed_for_a_date_only_request(
    authenticated_user, session, garage
):
    request_without_time = _pending_request(session, garage, preferred_time=None)
    (row,) = authenticated_user.client.get("/api/booking-requests/").get_json()
    assert row["id"] == str(request_without_time.id)
    assert row["slot_check"]["checked"] is False


def test_approved_request_reports_the_reviewers_name(
    authenticated_user, session, garage, booking_request
):
    authenticated_user.user.first_name = "Jamie"
    authenticated_user.user.last_name = "Lee"
    session.commit()
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

    body = authenticated_user.client.get(
        f"/api/booking-requests/{booking_request.id}"
    ).get_json()
    assert body["reviewed_by_name"] == "Jamie Lee"


# --------------------------------------------------------------------------
# Expiry (see app/booking_requests/service.py)
# --------------------------------------------------------------------------


def test_a_past_pending_request_is_swept_to_expired_on_list(
    authenticated_user, session, garage
):
    stale = _pending_request(
        session,
        garage,
        preferred_date=datetime.date.today() - datetime.timedelta(days=1),
        preferred_time=datetime.time(9, 0),
        customer_email="stale@example.com",
    )

    body = authenticated_user.client.get("/api/booking-requests/?status=PENDING").get_json()
    assert str(stale.id) not in {r["id"] for r in body}

    session.refresh(stale)
    assert stale.status == "EXPIRED"


def test_a_stale_same_day_request_is_swept_to_expired(authenticated_user, session, garage):
    now = datetime.datetime.now(datetime.UTC)
    stale = _pending_request(
        session,
        garage,
        preferred_date=now.date(),
        preferred_time=(now - datetime.timedelta(hours=1)).time(),
        customer_email="today-stale@example.com",
    )

    authenticated_user.client.get("/api/booking-requests/")

    session.refresh(stale)
    assert stale.status == "EXPIRED"


def test_a_future_pending_request_is_not_expired(authenticated_user, booking_request):
    authenticated_user.client.get("/api/booking-requests/")
    assert booking_request.status == "PENDING"


def test_expired_request_shows_is_expired_and_cannot_be_approved(
    authenticated_user, session, garage
):
    stale = _pending_request(
        session,
        garage,
        preferred_date=datetime.date.today() - datetime.timedelta(days=1),
        preferred_time=datetime.time(9, 0),
    )
    appt_type = _make_type(session, garage)

    detail = authenticated_user.client.get(
        f"/api/booking-requests/{stale.id}"
    ).get_json()
    assert detail["status"] == "EXPIRED"
    assert detail["is_expired"] is True

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{stale.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
        },
    )
    assert resp.status_code == 409
    assert Appointment.query.count() == 0


def test_expired_request_cannot_be_rejected_either(authenticated_user, session, garage):
    stale = _pending_request(
        session,
        garage,
        preferred_date=datetime.date.today() - datetime.timedelta(days=1),
        preferred_time=datetime.time(9, 0),
    )

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{stale.id}/reject", json={}
    )
    assert resp.status_code == 409

    session.refresh(stale)
    assert stale.status == "EXPIRED"


def test_expiring_a_request_releases_its_reserved_slot(
    authenticated_user, session, garage, garage_schedule
):
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    garage_schedule.capacity_per_slot = 1
    session.commit()
    _pending_request(
        session,
        garage,
        preferred_date=yesterday,
        preferred_time=datetime.time(9, 0),
        customer_email="held-slot@example.com",
    )

    # Sweeping is triggered by any staff-facing read.
    authenticated_user.client.get("/api/booking-requests/")

    # The (now past) day is reported as "past" regardless, but the important
    # thing is the request no longer counts as PENDING at all.
    from app.models.booking_request import BookingRequest as BR

    assert BR.query.filter_by(status="PENDING").count() == 0


def test_approved_and_rejected_history_is_untouched_by_the_sweep(
    authenticated_user, session, garage
):
    appt_type = _make_type(session, garage)
    approved = _pending_request(
        session, garage, customer_email="approved@example.com",
        preferred_date=datetime.date.today() + datetime.timedelta(days=2),
    )
    authenticated_user.client.post(
        f"/api/booking-requests/{approved.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": "2026-11-05T09:00:00+00:00",
            "end_time": "2026-11-05T09:30:00+00:00",
        },
    )
    old_reviewed_at = approved.reviewed_at

    authenticated_user.client.get("/api/booking-requests/")

    session.refresh(approved)
    assert approved.status == "APPROVED"
    assert approved.reviewed_at == old_reviewed_at


# --------------------------------------------------------------------------
# Capacity re-check at approval (see app/public_booking/availability.py)
# --------------------------------------------------------------------------


def test_approval_rejected_when_capacity_per_slot_is_already_full(
    authenticated_user, session, garage, garage_schedule
):
    """capacity_per_slot can be configured below the employee count - a specific
    employee having no conflict doesn't by itself mean the slot is free."""
    from werkzeug.security import generate_password_hash

    from app.models.employee import Employee

    second_employee = Employee(
        garage_id=garage.id,
        email="second-tech@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
    )
    session.add(second_employee)
    garage_schedule.capacity_per_slot = 1
    session.commit()
    appt_type = _make_type(session, garage, minutes=30)

    other_request = _pending_request(
        session,
        garage,
        customer_email="other-request@example.com",
        preferred_date=datetime.date(2026, 11, 3),
        preferred_time=datetime.time(9, 0),
    )
    authenticated_user.client.post(
        f"/api/booking-requests/{other_request.id}/approve",
        json={
            "employee_id": str(authenticated_user.user.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:30:00+00:00",
        },
    )

    second_request = _pending_request(
        session,
        garage,
        customer_email="second-request@example.com",
        preferred_date=datetime.date(2026, 11, 3),
        preferred_time=datetime.time(9, 0),
    )
    resp = authenticated_user.client.post(
        f"/api/booking-requests/{second_request.id}/approve",
        json={
            # A different (garage A) employee - no per-employee conflict -
            # but capacity_per_slot=1 is already spent by the first approval.
            "employee_id": str(second_employee.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:30:00+00:00",
        },
    )
    assert resp.status_code == 409
    session.refresh(second_request)
    assert second_request.status == "PENDING"


def test_approval_rejects_a_deactivated_employee(
    authenticated_user, session, garage, booking_request
):
    from werkzeug.security import generate_password_hash

    from app.models.employee import Employee

    deactivated = Employee(
        garage_id=garage.id,
        email="gone@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
        is_active=False,
    )
    session.add(deactivated)
    session.commit()
    appt_type = _make_type(session, garage)

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/approve",
        json={
            "employee_id": str(deactivated.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START,
            "end_time": "2026-11-03T09:45:00+00:00",
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Archived customer/vehicle are reactivated by an approval that reuses them
# --------------------------------------------------------------------------


def test_approve_reactivates_an_archived_customer_matched_by_email(
    authenticated_user, session, garage, booking_request
):
    existing = Customer(
        garage_id=garage.id,
        first_name="Pat",
        last_name="Rivera",
        email=booking_request.customer_email,
        is_active=False,
    )
    session.add(existing)
    session.commit()
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

    session.refresh(existing)
    assert existing.is_active is True
