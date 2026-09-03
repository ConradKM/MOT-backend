"""API tests for per-garage appointment status configuration.

Appointment.status stays a string; these endpoints just customise its
label/colour and let a garage add its own keys. A garage with no rows falls
back to the built-in default set.
"""


def _make_appointment(staff, customer, status=None):
    appt_type = staff.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    payload = {
        "employee_id": str(staff.user.id),
        "customer_id": str(customer.id),
        "appointment_type_id": appt_type["id"],
        "start_time": "2026-12-01T09:00:00+00:00",
        "end_time": "2026-12-01T10:00:00+00:00",
    }
    if status is not None:
        payload["status"] = status
    return staff.client.post("/api/appointments/", json=payload)


# --------------------------------------------------------------------------
# Seeding / listing
# --------------------------------------------------------------------------


def test_register_seeds_the_built_in_status_set(client):
    client.post(
        "/api/auth/register",
        json={"garage_name": "Fresh Garage", "email": "fresh@example.com", "password": "password-12"},
    )
    token = client.post(
        "/api/auth/login",
        json={"email": "fresh@example.com", "password": "password-12"},
    ).get_json()["access_token"]

    resp = client.get(
        "/api/appointment-statuses/", headers={"Authorization": f"Bearer {token}"}
    )
    keys = [s["key"] for s in resp.get_json()]
    assert keys == [
        "REQUESTED",
        "BOOKED",
        "IN_PROGRESS",
        "ACTION_NEEDED",
        "COMPLETED",
        "CANCELLED",
        "NO_SHOW",
    ]
    assert all(s["is_system"] for s in resp.get_json())


def test_list_is_garage_scoped(authenticated_user, seeded_statuses, second_authenticated_client):
    a = authenticated_user.client.get("/api/appointment-statuses/").get_json()
    b = second_authenticated_client.get("/api/appointment-statuses/").get_json()

    assert len(a) == 7
    assert b == []  # second garage hasn't been seeded in this test


# --------------------------------------------------------------------------
# Create / update / delete
# --------------------------------------------------------------------------


def test_owner_can_create_a_custom_status(authenticated_user, seeded_statuses):
    resp = authenticated_user.client.post(
        "/api/appointment-statuses/",
        json={"label": "Awaiting parts", "color": "orange", "sort_order": 45},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["key"] == "AWAITING_PARTS"
    assert body["is_system"] is False


def test_create_duplicate_key_conflicts(authenticated_user, seeded_statuses):
    authenticated_user.client.post(
        "/api/appointment-statuses/", json={"label": "On hold", "color": "grey"}
    )
    resp = authenticated_user.client.post(
        "/api/appointment-statuses/",
        json={"key": "ON_HOLD", "label": "On hold again", "color": "grey"},
    )
    assert resp.status_code == 409


def test_staff_cannot_create_a_status(client, garage, staff_role, session):
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token
    from app.models.employee import Employee

    staff = Employee(
        garage_id=garage.id,
        email="staff-s@example.com",
        password_hash=generate_password_hash("x"),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    token = create_access_token(identity=str(staff.id))

    resp = client.post(
        "/api/appointment-statuses/",
        json={"label": "Nope", "color": "red"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_owner_can_relabel_and_recolour_a_status(authenticated_user, seeded_statuses):
    booked = next(
        s
        for s in authenticated_user.client.get("/api/appointment-statuses/").get_json()
        if s["key"] == "BOOKED"
    )
    resp = authenticated_user.client.patch(
        f"/api/appointment-statuses/{booked['id']}",
        json={"label": "Confirmed", "color": "indigo"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["label"] == "Confirmed"
    assert body["color"] == "indigo"
    assert body["key"] == "BOOKED"  # key unchanged


def test_delete_custom_status(authenticated_user, seeded_statuses):
    made = authenticated_user.client.post(
        "/api/appointment-statuses/", json={"label": "Temp", "color": "grey"}
    ).get_json()

    resp = authenticated_user.client.delete(f"/api/appointment-statuses/{made['id']}")
    assert resp.status_code == 204


def test_cannot_delete_a_built_in_status(authenticated_user, seeded_statuses):
    booked = next(
        s
        for s in authenticated_user.client.get("/api/appointment-statuses/").get_json()
        if s["key"] == "BOOKED"
    )
    resp = authenticated_user.client.delete(f"/api/appointment-statuses/{booked['id']}")
    assert resp.status_code == 403


def test_cannot_delete_a_status_in_use(authenticated_user, seeded_statuses, customer):
    made = authenticated_user.client.post(
        "/api/appointment-statuses/", json={"label": "Waiting", "color": "grey"}
    ).get_json()
    assert _make_appointment(authenticated_user, customer, status="WAITING").status_code == 201

    resp = authenticated_user.client.delete(f"/api/appointment-statuses/{made['id']}")
    assert resp.status_code == 409


def test_cross_garage_status_is_404(
    authenticated_user, seeded_statuses, second_authenticated_client
):
    status_id = authenticated_user.client.get("/api/appointment-statuses/").get_json()[0]["id"]

    assert (
        second_authenticated_client.patch(
            f"/api/appointment-statuses/{status_id}", json={"label": "X"}
        ).status_code
        == 404
    )
    assert (
        second_authenticated_client.delete(
            f"/api/appointment-statuses/{status_id}"
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------
# Appointment status validation
# --------------------------------------------------------------------------


def test_appointment_accepts_a_configured_custom_status(
    authenticated_user, seeded_statuses, customer
):
    authenticated_user.client.post(
        "/api/appointment-statuses/", json={"label": "Awaiting parts", "color": "orange"}
    )
    resp = _make_appointment(authenticated_user, customer, status="AWAITING_PARTS")
    assert resp.status_code == 201


def test_appointment_rejects_an_unknown_status(authenticated_user, customer):
    resp = _make_appointment(authenticated_user, customer, status="TOTALLY_MADE_UP")
    assert resp.status_code == 422


def test_appointment_still_accepts_default_statuses_without_seeding(
    authenticated_user, customer
):
    # authenticated_user's garage has no status rows -> falls back to defaults.
    resp = _make_appointment(authenticated_user, customer, status="IN_PROGRESS")
    assert resp.status_code == 201
