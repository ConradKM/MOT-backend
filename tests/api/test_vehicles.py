"""API tests for /api/vehicles/ and /api/vehicles/{id}."""


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_create_vehicle(authenticated_user, customer):
    resp = authenticated_user.client.post(
        "/api/vehicles/",
        json={
            "customer_id": customer.id,
            "registration_number": "AB12CDE",
            "make": "Ford",
            "model": "Focus",
            "year": 2020,
            "current_mileage": 10000,
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["registration_number"] == "AB12CDE"
    assert body["customer_id"] == customer.id
    assert body["make"] == "Ford"


def test_retrieve_vehicle(authenticated_user, vehicle):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == vehicle.id


def test_list_vehicles(authenticated_user, vehicle):
    resp = authenticated_user.client.get("/api/vehicles/")

    assert resp.status_code == 200
    ids = {v["id"] for v in resp.get_json()}
    assert vehicle.id in ids


def test_list_vehicles_filter_by_registration(authenticated_user, vehicle):
    resp = authenticated_user.client.get(
        "/api/vehicles/", query_string={"registration": "ab12"}
    )

    assert resp.status_code == 200
    assert any(v["id"] == vehicle.id for v in resp.get_json())


def test_list_vehicles_filter_by_customer_id(authenticated_user, vehicle, customer):
    resp = authenticated_user.client.get(
        "/api/vehicles/", query_string={"customer_id": customer.id}
    )

    assert resp.status_code == 200
    assert all(v["customer_id"] == customer.id for v in resp.get_json())


def test_update_vehicle(authenticated_user, vehicle):
    resp = authenticated_user.client.patch(
        f"/api/vehicles/{vehicle.id}", json={"current_mileage": 25000}
    )

    assert resp.status_code == 200
    assert resp.get_json()["current_mileage"] == 25000


def test_delete_vehicle(authenticated_user, vehicle):
    resp = authenticated_user.client.delete(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code == 204

    get_resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert get_resp.status_code == 404


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_create_vehicle_missing_required_fields(authenticated_user):
    resp = authenticated_user.client.post("/api/vehicles/", json={})

    assert resp.status_code == 422
    errors = resp.get_json()["errors"]["json"]
    assert "customer_id" in errors
    assert "registration_number" in errors


def test_create_vehicle_invalid_customer_id_type(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/vehicles/",
        json={"customer_id": "not-an-int", "registration_number": "AB12CDE"},
    )
    assert resp.status_code == 422


def test_create_vehicle_customer_id_not_belonging_to_garage(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/vehicles/",
        json={"customer_id": 999999, "registration_number": "AB12CDE"},
    )
    assert resp.status_code == 422


def test_create_vehicle_duplicate_registration_conflicts(authenticated_user, customer, vehicle):
    resp = authenticated_user.client.post(
        "/api/vehicles/",
        json={"customer_id": customer.id, "registration_number": vehicle.registration_number},
    )
    assert resp.status_code == 409


def test_retrieve_vehicle_invalid_id_returns_404(authenticated_user):
    resp = authenticated_user.client.get("/api/vehicles/999999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_list_vehicles_requires_auth(client):
    assert client.get("/api/vehicles/").status_code == 401


def test_create_vehicle_requires_auth(client, customer):
    resp = client.post(
        "/api/vehicles/",
        json={"customer_id": customer.id, "registration_number": "AB12CDE"},
    )
    assert resp.status_code == 401


def test_get_vehicle_requires_auth(client, vehicle):
    assert client.get(f"/api/vehicles/{vehicle.id}").status_code == 401


def test_update_vehicle_requires_auth(client, vehicle):
    resp = client.patch(f"/api/vehicles/{vehicle.id}", json={"current_mileage": 1})
    assert resp.status_code == 401


def test_delete_vehicle_requires_auth(client, vehicle):
    assert client.delete(f"/api/vehicles/{vehicle.id}").status_code == 401


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def test_vehicle_customer_relationship_is_returned(authenticated_user, vehicle, customer):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert resp.get_json()["customer_id"] == customer.id


def test_vehicle_garage_relationship_is_scoped(authenticated_user, vehicle):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert resp.get_json()["garage_id"] == vehicle.garage_id


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_user_a_cannot_view_garage_b_vehicles(authenticated_user, vehicle, second_vehicle):
    resp = authenticated_user.client.get("/api/vehicles/")
    ids = {v["id"] for v in resp.get_json()}

    assert vehicle.id in ids
    assert second_vehicle.id not in ids


def test_user_a_cannot_create_vehicle_against_garage_b_customer(
    authenticated_user, second_customer
):
    resp = authenticated_user.client.post(
        "/api/vehicles/",
        json={"customer_id": second_customer.id, "registration_number": "AB12CDE"},
    )
    assert resp.status_code == 422


def test_user_a_cannot_modify_garage_b_vehicle(authenticated_user, second_vehicle, session):
    resp = authenticated_user.client.patch(
        f"/api/vehicles/{second_vehicle.id}", json={"current_mileage": 1}
    )

    assert resp.status_code == 404
    session.refresh(second_vehicle)
    assert second_vehicle.current_mileage != 1


def test_user_a_cannot_delete_garage_b_vehicle(authenticated_user, second_vehicle, session):
    resp = authenticated_user.client.delete(f"/api/vehicles/{second_vehicle.id}")

    assert resp.status_code == 404
    assert session.get(type(second_vehicle), second_vehicle.id) is not None
