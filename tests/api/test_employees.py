"""API tests for GET/POST /api/employees/."""


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_list_employees_includes_the_owner(authenticated_user):
    resp = authenticated_user.client.get("/api/employees/")

    assert resp.status_code == 200
    emails = {e["email"] for e in resp.get_json()}
    assert authenticated_user.user.email in emails


def test_owner_can_create_an_employee(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "staff@garage-a.example", "password": "password123"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["email"] == "staff@garage-a.example"
    assert body["garage_id"] == str(authenticated_user.garage.id)
    assert "id" in body
    assert "created_at" in body
    assert "updated_at" in body


def test_employee_can_be_created_with_a_name(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "named@garage-a.example",
            "password": "password123",
            "first_name": "Jane",
            "last_name": "Doe",
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["first_name"] == "Jane"
    assert body["last_name"] == "Doe"


def test_employee_name_is_optional(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "unnamed@garage-a.example", "password": "password123"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["first_name"] is None
    assert body["last_name"] is None


def test_owner_can_edit_employee_name(authenticated_user, staff_role):
    employee = _create_staff(authenticated_user)

    resp = authenticated_user.client.patch(
        f"/api/employees/{employee['id']}",
        json={"first_name": "Renamed", "last_name": "Person"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["first_name"] == "Renamed"
    assert body["last_name"] == "Person"


def test_created_employee_defaults_to_staff_role(authenticated_user, staff_role):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "staff-default@garage-a.example", "password": "password123"},
    )

    role_names = {r["name"] for r in resp.get_json()["roles"]}
    assert role_names == {"STAFF"}


def test_owner_can_create_a_second_owner(authenticated_user):
    owner_role_id = authenticated_user.user.roles[0].id

    resp = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "co-owner@garage-a.example",
            "password": "password123",
            "role_ids": [str(owner_role_id)],
        },
    )

    assert resp.status_code == 201
    role_names = {r["name"] for r in resp.get_json()["roles"]}
    assert role_names == {"OWNER"}


def test_employee_can_be_created_with_multiple_roles(authenticated_user, staff_role):
    owner_role_id = authenticated_user.user.roles[0].id

    resp = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "multi-role@garage-a.example",
            "password": "password123",
            "role_ids": [str(owner_role_id), str(staff_role.id)],
        },
    )

    assert resp.status_code == 201
    role_names = {r["name"] for r in resp.get_json()["roles"]}
    assert role_names == {"OWNER", "STAFF"}


def test_created_employee_does_not_leak_password(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "no-leak@garage-a.example", "password": "password123"},
    )
    body = resp.get_json()

    assert "password" not in body
    assert "password_hash" not in body


def test_new_employee_can_log_in(authenticated_user):
    authenticated_user.client.post(
        "/api/employees/",
        json={"email": "loginable@garage-a.example", "password": "password123"},
    )

    resp = authenticated_user.client.post(
        "/api/auth/login",
        json={"email": "loginable@garage-a.example", "password": "password123"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["access_token"]


def test_new_staff_employee_can_list_but_not_create(authenticated_user, client):
    authenticated_user.client.post(
        "/api/employees/",
        json={"email": "junior@garage-a.example", "password": "password123"},
    )
    login = authenticated_user.client.post(
        "/api/auth/login",
        json={"email": "junior@garage-a.example", "password": "password123"},
    ).get_json()
    # Use the raw client here, not authenticated_user.client - that wrapper
    # always re-asserts its own bound (owner) token over any headers passed
    # in, so it can't be used to act as a different identity.
    staff_headers = {"Authorization": f"Bearer {login['access_token']}"}

    list_resp = client.get("/api/employees/", headers=staff_headers)
    assert list_resp.status_code == 200

    create_resp = client.post(
        "/api/employees/",
        json={"email": "blocked@garage-a.example", "password": "password123"},
        headers=staff_headers,
    )
    assert create_resp.status_code == 403


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_create_employee_missing_email(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"password": "password123"}
    )

    assert resp.status_code == 422
    assert "email" in resp.get_json()["errors"]["json"]


def test_create_employee_missing_password(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"email": "nopass@garage-a.example"}
    )

    assert resp.status_code == 422
    assert "password" in resp.get_json()["errors"]["json"]


def test_create_employee_invalid_email(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"email": "not-an-email", "password": "password123"}
    )

    assert resp.status_code == 422
    assert "email" in resp.get_json()["errors"]["json"]


def test_create_employee_short_password(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"email": "shortpw@garage-a.example", "password": "short"}
    )

    assert resp.status_code == 422
    assert "password" in resp.get_json()["errors"]["json"]


def test_create_employee_unknown_role_id(authenticated_user):
    import uuid

    resp = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "badrole@garage-a.example",
            "password": "password123",
            "role_ids": [str(uuid.uuid4())],
        },
    )

    assert resp.status_code == 422


def test_create_employee_cannot_use_another_garages_role_id(
    authenticated_user, second_owner_role
):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "cross-tenant-role@garage-a.example",
            "password": "password123",
            "role_ids": [str(second_owner_role.id)],
        },
    )

    assert resp.status_code == 422


def test_create_employee_duplicate_email(authenticated_user, user):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"email": user.email, "password": "password123"}
    )

    assert resp.status_code == 409
    assert "message" in resp.get_json()


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_list_employees_requires_auth(client):
    assert client.get("/api/employees/").status_code == 401


def test_create_employee_requires_auth(client):
    resp = client.post(
        "/api/employees/", json={"email": "x@example.com", "password": "password123"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_garage_a_only_sees_its_own_employees(
    authenticated_user, second_authenticated_client, second_user
):
    authenticated_user.client.post(
        "/api/employees/",
        json={"email": "isolated-a@garage-a.example", "password": "password123"},
    )

    resp_a = authenticated_user.client.get("/api/employees/")
    resp_b = second_authenticated_client.get("/api/employees/")

    emails_a = {e["email"] for e in resp_a.get_json()}
    emails_b = {e["email"] for e in resp_b.get_json()}

    assert "isolated-a@garage-a.example" in emails_a
    assert second_user.email not in emails_a
    assert second_user.email in emails_b
    assert "isolated-a@garage-a.example" not in emails_b


def test_employee_created_in_garage_a_belongs_to_garage_a(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "scoped@garage-a.example", "password": "password123"},
    )

    assert resp.get_json()["garage_id"] == str(authenticated_user.garage.id)


def test_user_b_cannot_create_employee_in_garage_a(
    authenticated_user, second_authenticated_client, session
):
    # There's no garage_id field on the request at all - Garage B's owner
    # can only ever create employees in their own garage.
    resp = second_authenticated_client.post(
        "/api/employees/",
        json={"email": "still-garage-b@garage-b.example", "password": "password123"},
    )

    assert resp.status_code == 201
    assert resp.get_json()["garage_id"] != str(authenticated_user.garage.id)


# --------------------------------------------------------------------------
# PATCH /api/employees/<id>
# --------------------------------------------------------------------------


def _create_staff(authenticated_user, email="junior@garage-a.example"):
    resp = authenticated_user.client.post(
        "/api/employees/", json={"email": email, "password": "password123"}
    )
    return resp.get_json()


def test_owner_can_edit_employee_email(authenticated_user, staff_role):
    employee = _create_staff(authenticated_user)

    resp = authenticated_user.client.patch(
        f"/api/employees/{employee['id']}", json={"email": "renamed@garage-a.example"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["email"] == "renamed@garage-a.example"


def test_owner_can_edit_employee_roles(authenticated_user, staff_role):
    employee = _create_staff(authenticated_user)
    owner_role_id = str(authenticated_user.user.roles[0].id)

    resp = authenticated_user.client.patch(
        f"/api/employees/{employee['id']}", json={"role_ids": [owner_role_id]}
    )

    assert resp.status_code == 200
    role_names = {r["name"] for r in resp.get_json()["roles"]}
    assert role_names == {"OWNER"}


def test_editing_employee_requires_owner(authenticated_user, staff_role, client):
    employee = _create_staff(authenticated_user)
    login = authenticated_user.client.post(
        "/api/auth/login",
        json={"email": employee["email"], "password": "password123"},
    ).get_json()
    staff_headers = {"Authorization": f"Bearer {login['access_token']}"}

    resp = client.patch(
        f"/api/employees/{employee['id']}",
        json={"email": "self-promoted@garage-a.example"},
        headers=staff_headers,
    )

    assert resp.status_code == 403


def test_edit_employee_requires_auth(client, authenticated_user, staff_role):
    employee = _create_staff(authenticated_user)

    resp = client.patch(
        f"/api/employees/{employee['id']}", json={"email": "nope@garage-a.example"}
    )

    assert resp.status_code == 401


def test_edit_unknown_employee_returns_404(authenticated_user):
    import uuid

    resp = authenticated_user.client.patch(
        f"/api/employees/{uuid.uuid4()}", json={"email": "ghost@garage-a.example"}
    )

    assert resp.status_code == 404


def test_edit_employee_email_conflict(authenticated_user, staff_role):
    employee = _create_staff(authenticated_user)

    resp = authenticated_user.client.patch(
        f"/api/employees/{employee['id']}", json={"email": authenticated_user.user.email}
    )

    assert resp.status_code == 409


def test_owner_cannot_remove_their_own_last_owner_role(authenticated_user):
    resp = authenticated_user.client.patch(
        f"/api/employees/{authenticated_user.user.id}", json={"role_ids": []}
    )

    assert resp.status_code == 409


def test_owner_can_remove_own_owner_role_if_another_owner_exists(authenticated_user):
    owner_role_id = str(authenticated_user.user.roles[0].id)
    second_owner = authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "co-owner@garage-a.example",
            "password": "password123",
            "role_ids": [owner_role_id],
        },
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/employees/{authenticated_user.user.id}", json={"role_ids": []}
    )

    assert resp.status_code == 200
    assert resp.get_json()["roles"] == []
    assert second_owner["roles"][0]["name"] == "OWNER"


# --------------------------------------------------------------------------
# Activate / deactivate
# --------------------------------------------------------------------------


def test_new_employee_is_active_by_default(authenticated_user):
    created = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "active@garage-a.example", "password": "password123"},
    ).get_json()
    assert created["is_active"] is True


def test_owner_can_deactivate_and_reactivate_an_employee(authenticated_user):
    emp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "toggle@garage-a.example", "password": "password123"},
    ).get_json()

    off = authenticated_user.client.patch(
        f"/api/employees/{emp['id']}", json={"is_active": False}
    )
    assert off.status_code == 200 and off.get_json()["is_active"] is False

    on = authenticated_user.client.patch(
        f"/api/employees/{emp['id']}", json={"is_active": True}
    )
    assert on.status_code == 200 and on.get_json()["is_active"] is True


def test_deactivated_employee_cannot_log_in(authenticated_user):
    authenticated_user.client.post(
        "/api/employees/",
        json={"email": "gone@garage-a.example", "password": "password123"},
    )
    emp_id = [
        e["id"]
        for e in authenticated_user.client.get("/api/employees/").get_json()
        if e["email"] == "gone@garage-a.example"
    ][0]
    authenticated_user.client.patch(f"/api/employees/{emp_id}", json={"is_active": False})

    resp = authenticated_user.client.post(
        "/api/auth/login",
        json={"email": "gone@garage-a.example", "password": "password123"},
    )
    assert resp.status_code == 401


def test_owner_cannot_deactivate_the_only_active_owner(authenticated_user):
    resp = authenticated_user.client.patch(
        f"/api/employees/{authenticated_user.user.id}", json={"is_active": False}
    )
    assert resp.status_code == 409


def test_owner_can_deactivate_self_when_another_active_owner_exists(authenticated_user):
    owner_role_id = str(authenticated_user.user.roles[0].id)
    authenticated_user.client.post(
        "/api/employees/",
        json={
            "email": "co-owner2@garage-a.example",
            "password": "password123",
            "role_ids": [owner_role_id],
        },
    )
    resp = authenticated_user.client.patch(
        f"/api/employees/{authenticated_user.user.id}", json={"is_active": False}
    )
    assert resp.status_code == 200


def test_a_normal_employee_cannot_toggle_activation(
    authenticated_user, staff_role, client
):
    from werkzeug.security import generate_password_hash

    from app.extensions import db
    from app.models.employee import Employee

    staff = Employee(
        garage_id=authenticated_user.garage.id,
        email="plainstaff@garage-a.example",
        password_hash=generate_password_hash("password123"),
        roles=[staff_role],
    )
    db.session.add(staff)
    db.session.commit()

    token = client.post(
        "/api/auth/login",
        json={"email": "plainstaff@garage-a.example", "password": "password123"},
    ).get_json()["access_token"]

    resp = client.patch(
        f"/api/employees/{authenticated_user.user.id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
