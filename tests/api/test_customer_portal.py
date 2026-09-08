"""API tests for the read-only customer portal.

GET /api/customer/account and GET /api/customer/appointments/<id> return only
the signed-in customer's own records, and reject staff / anonymous callers.
"""

import datetime

from app.models.booking_request import BookingRequest

_START = "2026-10-01T{hour:02d}:00:00+00:00"
_END = "2026-10-01T{hour:02d}:30:00+00:00"


def _make_pending_request(session, garage, customer, *, reference="BK1234567", days_ahead=3):
    br = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference=reference,
        customer_id=customer.id,
        customer_first_name=customer.first_name,
        customer_last_name=customer.last_name,
        customer_email=customer.email,
        vehicle_registration="AB12CDE",
        preferred_date=datetime.datetime.now(datetime.UTC).date()
        + datetime.timedelta(days=days_ahead),
    )
    session.add(br)
    session.commit()
    return br


def _create_appointment(staff, *, customer_id, vehicle_id=None, hour=9, name="MOT"):
    """Create an appointment for `customer_id` via the staff API. Returns its JSON."""
    appt_type = staff.client.post("/api/appointment-types/", json={"name": name}).get_json()

    payload = {
        "employee_id": str(staff.user.id),
        "customer_id": str(customer_id),
        "appointment_type_id": appt_type["id"],
        "start_time": _START.format(hour=hour),
        "end_time": _END.format(hour=hour),
    }
    if vehicle_id is not None:
        payload["vehicle_id"] = str(vehicle_id)

    resp = staff.client.post("/api/appointments/", json=payload)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _add_customer_with_vehicle(staff, *, email="other@example.com", reg="OT11HER"):
    customer = staff.client.post(
        "/api/customers/",
        json={"first_name": "Other", "last_name": "Person", "email": email},
    ).get_json()
    vehicle = staff.client.post(
        "/api/vehicles/",
        json={"customer_id": customer["id"], "registration_number": reg},
    ).get_json()
    return customer, vehicle


# --------------------------------------------------------------------------
# GET /api/customer/account
# --------------------------------------------------------------------------


def test_account_returns_the_signed_in_customers_profile(customer_client, customer, garage):
    resp = customer_client.get("/api/customer/account")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["customer"]["id"] == str(customer.id)
    assert body["customer"]["garage_name"] == garage.name


def test_account_lists_vehicles_with_mot_history(customer_client, vehicle, mot_record):
    body = customer_client.get("/api/customer/account").get_json()

    regs = [v["registration_number"] for v in body["vehicles"]]
    assert vehicle.registration_number in regs

    car = next(
        v for v in body["vehicles"] if v["registration_number"] == vehicle.registration_number
    )
    assert len(car["mot_records"]) == 1
    assert car["mot_records"][0]["result"] == "PASS"


def test_account_lists_the_customers_appointments(
    customer_client, authenticated_user, customer, vehicle
):
    appt = _create_appointment(authenticated_user, customer_id=customer.id, vehicle_id=vehicle.id)

    body = customer_client.get("/api/customer/account").get_json()

    ids = [a["id"] for a in body["appointments"]]
    assert appt["id"] in ids
    mine = next(a for a in body["appointments"] if a["id"] == appt["id"])
    assert mine["appointment_type_name"] == "MOT"
    assert mine["vehicle_registration"] == vehicle.registration_number


def test_account_lists_a_pending_booking_request(customer_client, session, garage, customer):
    # No Appointment exists for a PENDING request - see
    # app/booking_requests/routes.py::BookingRequestApprove - so this is what
    # the account page shows in its place until one does.
    pending = _make_pending_request(session, garage, customer)

    body = customer_client.get("/api/customer/account").get_json()

    ids = [r["id"] for r in body["pending_requests"]]
    assert str(pending.id) in ids
    mine = next(r for r in body["pending_requests"] if r["id"] == str(pending.id))
    assert mine["booking_reference"] == "BK1234567"
    assert mine["vehicle_registration"] == "AB12CDE"
    # Not yet an appointment, so it must not also show up as one.
    assert all(a["id"] != str(pending.id) for a in body["appointments"])


def test_account_excludes_an_approved_requests_pending_entry(
    customer_client, authenticated_user, session, garage, customer, vehicle
):
    # Once approved it has become an appointment (covered by
    # test_account_lists_the_customers_appointments) - it must not also
    # linger in pending_requests.
    pending = _make_pending_request(session, garage, customer)
    pending.status = "APPROVED"
    session.commit()

    body = customer_client.get("/api/customer/account").get_json()

    assert all(r["id"] != str(pending.id) for r in body["pending_requests"])


def test_account_excludes_other_customers_data(customer_client, authenticated_user, customer):
    _, _ = _add_customer_with_vehicle(authenticated_user)
    other_customer, other_vehicle = _add_customer_with_vehicle(
        authenticated_user, email="third@example.com", reg="TH1RD01"
    )
    other_appt = _create_appointment(
        authenticated_user,
        customer_id=other_customer["id"],
        vehicle_id=other_vehicle["id"],
        hour=11,
    )

    body = customer_client.get("/api/customer/account").get_json()

    assert all(v["registration_number"] != "TH1RD01" for v in body["vehicles"])
    assert all(a["id"] != other_appt["id"] for a in body["appointments"])


def test_account_rejects_an_employee_token(authenticated_user):
    resp = authenticated_user.client.get("/api/customer/account")
    assert resp.status_code in (401, 403)


def test_account_requires_a_token(client):
    resp = client.get("/api/customer/account")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# GET /api/customer/appointments/<id>
# --------------------------------------------------------------------------


def test_appointment_detail_for_own_appointment(
    customer_client, authenticated_user, customer, vehicle
):
    appt = _create_appointment(authenticated_user, customer_id=customer.id, vehicle_id=vehicle.id)

    resp = customer_client.get(f"/api/customer/appointments/{appt['id']}")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == appt["id"]
    assert body["appointment_type_name"] == "MOT"
    assert body["vehicle"]["registration_number"] == vehicle.registration_number
    assert body["garage_name"]


def test_appointment_detail_for_another_customers_appointment_is_404(
    customer_client, authenticated_user
):
    other_customer, other_vehicle = _add_customer_with_vehicle(authenticated_user)
    other_appt = _create_appointment(
        authenticated_user,
        customer_id=other_customer["id"],
        vehicle_id=other_vehicle["id"],
        hour=13,
    )

    resp = customer_client.get(f"/api/customer/appointments/{other_appt['id']}")
    assert resp.status_code == 404


def test_appointment_detail_unknown_id_is_404(customer_client):
    resp = customer_client.get("/api/customer/appointments/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_appointment_detail_rejects_an_employee_token(authenticated_user, customer, vehicle):
    appt = _create_appointment(authenticated_user, customer_id=customer.id, vehicle_id=vehicle.id)

    resp = authenticated_user.client.get(f"/api/customer/appointments/{appt['id']}")
    assert resp.status_code in (401, 403)
