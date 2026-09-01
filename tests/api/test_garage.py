"""API tests for GET /api/garage, PATCH /api/garage."""

import uuid


def test_authenticated_user_can_retrieve_their_garage(authenticated_user):
    resp = authenticated_user.client.get("/api/garage")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == str(authenticated_user.garage.id)
    assert resp.get_json()["name"] == authenticated_user.garage.name


def test_unauthenticated_user_receives_auth_error(client):
    resp = client.get("/api/garage")
    assert resp.status_code == 401


def test_user_can_update_allowed_garage_fields(authenticated_user):
    resp = authenticated_user.client.patch(
        "/api/garage",
        json={"name": "Renamed Garage", "phone": "+44 20 0000 0000"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Renamed Garage"
    assert body["phone"] == "+44 20 0000 0000"


def test_updated_information_is_persisted(authenticated_user, session):
    authenticated_user.client.patch("/api/garage", json={"name": "Persisted Name"})

    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.name == "Persisted Name"


def test_patch_rejects_unknown_identity_fields(authenticated_user):
    # GarageUpdateSchema has no `id` field, and marshmallow rejects unknown
    # fields by default, so attempting to set the garage's identity is a
    # validation error rather than a silent no-op.
    resp = authenticated_user.client.patch(
        "/api/garage",
        json={"id": str(uuid.uuid4()), "name": "Still Mine"},
    )

    assert resp.status_code == 422
    assert "id" in resp.get_json()["errors"]["json"]


def test_patch_unauthenticated_fails(client):
    resp = client.patch("/api/garage", json={"name": "Nope"})
    assert resp.status_code == 401


def test_staff_role_cannot_update_garage(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.patch("/api/garage", json={"name": "Staff Update"})

    assert resp.status_code == 403


def test_user_from_garage_a_only_ever_sees_their_own_garage(
    authenticated_user, second_authenticated_client, second_garage
):
    resp_a = authenticated_user.client.get("/api/garage")
    resp_b = second_authenticated_client.get("/api/garage")

    assert resp_a.get_json()["id"] == str(authenticated_user.garage.id)
    assert resp_b.get_json()["id"] == str(second_garage.id)
    assert resp_a.get_json()["id"] != resp_b.get_json()["id"]


def test_user_b_updates_do_not_affect_garage_a(
    authenticated_user, second_authenticated_client, session
):
    second_authenticated_client.patch("/api/garage", json={"name": "Garage B Renamed"})

    session.refresh(authenticated_user.garage)
    assert authenticated_user.garage.name != "Garage B Renamed"
