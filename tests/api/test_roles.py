"""API tests for GET/POST /api/roles/, PATCH/DELETE /api/roles/<id>."""

import uuid


def test_owner_can_list_roles(authenticated_user, staff_role):
    resp = authenticated_user.client.get("/api/roles/")

    assert resp.status_code == 200
    names = {r["name"] for r in resp.get_json()}
    assert names == {"OWNER", "STAFF"}


def test_list_roles_requires_auth(client):
    assert client.get("/api/roles/").status_code == 401


def test_owner_can_create_a_role(authenticated_user):
    resp = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Mechanic"
    assert body["garage_id"] == str(authenticated_user.garage.id)


def test_staff_cannot_create_a_role(authenticated_user, staff_role, client):
    staff = authenticated_user.client.post(
        "/api/employees/", json={"email": "staff@garage-a.example", "password": "password123"}
    ).get_json()
    login = authenticated_user.client.post(
        "/api/auth/login", json={"email": staff["email"], "password": "password123"}
    ).get_json()

    resp = client.post(
        "/api/roles/",
        json={"name": "Mechanic"},
        headers={"Authorization": f"Bearer {login['access_token']}"},
    )

    assert resp.status_code == 403


def test_create_role_requires_name(authenticated_user):
    resp = authenticated_user.client.post("/api/roles/", json={})

    assert resp.status_code == 422


def test_duplicate_role_name_in_same_garage_is_rejected(authenticated_user):
    authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"})
    resp = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"})

    assert resp.status_code == 409


def test_same_role_name_allowed_in_different_garages(
    authenticated_user, second_authenticated_client
):
    resp_a = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"})
    resp_b = second_authenticated_client.post("/api/roles/", json={"name": "Mechanic"})

    assert resp_a.status_code == 201
    assert resp_b.status_code == 201


def test_owner_can_rename_a_custom_role(authenticated_user):
    role = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"}).get_json()

    resp = authenticated_user.client.patch(
        f"/api/roles/{role['id']}", json={"name": "Senior Mechanic"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Senior Mechanic"


def test_owner_role_cannot_be_renamed(authenticated_user):
    owner_role_id = authenticated_user.user.roles[0].id

    resp = authenticated_user.client.patch(
        f"/api/roles/{owner_role_id}", json={"name": "Boss"}
    )

    assert resp.status_code == 403


def test_owner_role_cannot_be_deleted(authenticated_user):
    owner_role_id = authenticated_user.user.roles[0].id

    resp = authenticated_user.client.delete(f"/api/roles/{owner_role_id}")

    assert resp.status_code == 403


def test_owner_can_delete_a_custom_role(authenticated_user):
    role = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"}).get_json()

    resp = authenticated_user.client.delete(f"/api/roles/{role['id']}")
    assert resp.status_code == 204

    list_resp = authenticated_user.client.get("/api/roles/")
    names = {r["name"] for r in list_resp.get_json()}
    assert "Mechanic" not in names


def test_deleting_a_role_unassigns_it_from_employees(authenticated_user, staff_role):
    employee = authenticated_user.client.post(
        "/api/employees/", json={"email": "staff@garage-a.example", "password": "password123"}
    ).get_json()
    assert employee["roles"][0]["name"] == "STAFF"

    authenticated_user.client.delete(f"/api/roles/{staff_role.id}")

    resp = authenticated_user.client.get(f"/api/employees/{employee['id']}")
    assert resp.get_json()["roles"] == []


def test_edit_unknown_role_returns_404(authenticated_user):
    resp = authenticated_user.client.patch(
        f"/api/roles/{uuid.uuid4()}", json={"name": "Ghost"}
    )

    assert resp.status_code == 404


def test_user_b_cannot_edit_garage_as_role(
    authenticated_user, second_authenticated_client
):
    role = authenticated_user.client.post("/api/roles/", json={"name": "Mechanic"}).get_json()

    resp = second_authenticated_client.patch(
        f"/api/roles/{role['id']}", json={"name": "Hijacked"}
    )

    assert resp.status_code == 404
