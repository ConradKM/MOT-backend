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


def test_created_employee_defaults_to_staff_role(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "staff-default@garage-a.example", "password": "password123"},
    )

    assert resp.get_json()["role"] == "STAFF"


def test_owner_can_create_a_second_owner(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "co-owner@garage-a.example", "password": "password123", "role": "OWNER"},
    )

    assert resp.status_code == 201
    assert resp.get_json()["role"] == "OWNER"


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


def test_create_employee_invalid_role(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/employees/",
        json={"email": "badrole@garage-a.example", "password": "password123", "role": "MANAGER"},
    )

    assert resp.status_code == 422
    assert "role" in resp.get_json()["errors"]["json"]


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
