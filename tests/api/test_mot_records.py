"""API tests for /api/vehicles/{vehicle_id}/mot-records/ and .../{id}.

Note: the implementation only exposes GET (list), POST, GET (single), and
PATCH for MOT records - there is no DELETE endpoint, so none is tested here.
"""


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_create_mot_record(authenticated_user, vehicle):
    resp = authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "PASS"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["mot_date"] == "2026-01-01"
    assert body["expiry_date"] == "2027-01-01"
    assert body["result"] == "PASS"
    assert body["vehicle_id"] == vehicle.id


def test_retrieve_mot_record(authenticated_user, vehicle, mot_record):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == mot_record.id


def test_list_mot_records(authenticated_user, vehicle, mot_record):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/")

    assert resp.status_code == 200
    ids = {r["id"] for r in resp.get_json()}
    assert mot_record.id in ids


def test_update_mot_record(authenticated_user, vehicle, mot_record):
    resp = authenticated_user.client.patch(
        f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}",
        json={"result": "FAIL", "notes": "Needs new tyres."},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "FAIL"
    assert body["notes"] == "Needs new tyres."


def test_multiple_mot_records_for_one_vehicle(authenticated_user, vehicle):
    authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2024-01-01", "expiry_date": "2025-01-01", "result": "PASS"},
    )
    authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2025-01-01", "expiry_date": "2026-01-01", "result": "PASS"},
    )

    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/")
    assert resp.status_code == 200
    assert len(resp.get_json()) == 2


def test_mot_record_vehicle_relationship(authenticated_user, vehicle, mot_record):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}")
    assert resp.get_json()["vehicle_id"] == vehicle.id


def test_mot_record_garage_relationship(authenticated_user, vehicle, mot_record, garage):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}")
    assert resp.get_json()["garage_id"] == garage.id


def test_mot_expiry_date_persistence(authenticated_user, vehicle):
    resp = authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-06-01", "expiry_date": "2027-06-01", "result": "PASS"},
    )
    record_id = resp.get_json()["id"]

    get_resp = authenticated_user.client.get(
        f"/api/vehicles/{vehicle.id}/mot-records/{record_id}"
    )
    assert get_resp.get_json()["expiry_date"] == "2027-06-01"


def test_creating_mot_record_updates_vehicle_current_mot_expiry(authenticated_user, vehicle):
    authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-06-15", "result": "PASS"},
    )

    vehicle_resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert vehicle_resp.get_json()["mot_expiry_date"] == "2027-06-15"


def test_updating_mot_record_expiry_updates_vehicle_current_mot_expiry(
    authenticated_user, vehicle, mot_record
):
    resp = authenticated_user.client.patch(
        f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}",
        json={"expiry_date": "2028-01-01"},
    )
    assert resp.status_code == 200

    vehicle_resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert vehicle_resp.get_json()["mot_expiry_date"] == "2028-01-01"


def test_vehicle_current_mot_expiry_reflects_latest_of_multiple_records(
    authenticated_user, vehicle
):
    authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2024-01-01", "expiry_date": "2025-01-01", "result": "PASS"},
    )
    authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2025-01-01", "expiry_date": "2026-06-01", "result": "PASS"},
    )

    vehicle_resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}")
    assert vehicle_resp.get_json()["mot_expiry_date"] == "2026-06-01"


# --------------------------------------------------------------------------
# Invalid cases
# --------------------------------------------------------------------------


def test_create_mot_record_invalid_vehicle_id_returns_404(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/vehicles/999999/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "PASS"},
    )
    assert resp.status_code == 404


def test_retrieve_mot_record_invalid_record_id_returns_404(authenticated_user, vehicle):
    resp = authenticated_user.client.get(f"/api/vehicles/{vehicle.id}/mot-records/999999")
    assert resp.status_code == 404


def test_mot_record_belonging_to_another_vehicle_returns_404(
    authenticated_user, vehicle, second_vehicle
):
    create_resp = authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "PASS"},
    )
    record_id = create_resp.get_json()["id"]

    resp = authenticated_user.client.get(
        f"/api/vehicles/{second_vehicle.id}/mot-records/{record_id}"
    )
    assert resp.status_code == 404


def test_mot_record_belonging_to_another_garage_returns_404(
    authenticated_user, second_vehicle, second_mot_record
):
    resp = authenticated_user.client.get(
        f"/api/vehicles/{second_vehicle.id}/mot-records/{second_mot_record.id}"
    )
    assert resp.status_code == 404


def test_create_mot_record_invalid_dates(authenticated_user, vehicle):
    resp = authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "not-a-date", "expiry_date": "2027-01-01", "result": "PASS"},
    )
    assert resp.status_code == 422


def test_create_mot_record_missing_required_fields(authenticated_user, vehicle):
    resp = authenticated_user.client.post(f"/api/vehicles/{vehicle.id}/mot-records/", json={})

    assert resp.status_code == 422
    errors = resp.get_json()["errors"]["json"]
    assert "mot_date" in errors
    assert "expiry_date" in errors
    assert "result" in errors


def test_create_mot_record_invalid_result_value(authenticated_user, vehicle):
    resp = authenticated_user.client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "MAYBE"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_list_mot_records_requires_auth(client, vehicle):
    assert client.get(f"/api/vehicles/{vehicle.id}/mot-records/").status_code == 401


def test_create_mot_record_requires_auth(client, vehicle):
    resp = client.post(
        f"/api/vehicles/{vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "PASS"},
    )
    assert resp.status_code == 401


def test_get_mot_record_requires_auth(client, vehicle, mot_record):
    resp = client.get(f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}")
    assert resp.status_code == 401


def test_update_mot_record_requires_auth(client, vehicle, mot_record):
    resp = client.patch(
        f"/api/vehicles/{vehicle.id}/mot-records/{mot_record.id}", json={"result": "FAIL"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_user_a_cannot_list_garage_b_vehicle_mot_records(authenticated_user, second_vehicle):
    resp = authenticated_user.client.get(f"/api/vehicles/{second_vehicle.id}/mot-records/")
    assert resp.status_code == 404


def test_user_a_cannot_create_mot_record_for_garage_b_vehicle(authenticated_user, second_vehicle):
    resp = authenticated_user.client.post(
        f"/api/vehicles/{second_vehicle.id}/mot-records/",
        json={"mot_date": "2026-01-01", "expiry_date": "2027-01-01", "result": "PASS"},
    )
    assert resp.status_code == 404


def test_user_a_cannot_retrieve_garage_b_mot_record(
    authenticated_user, second_vehicle, second_mot_record
):
    resp = authenticated_user.client.get(
        f"/api/vehicles/{second_vehicle.id}/mot-records/{second_mot_record.id}"
    )
    assert resp.status_code == 404


def test_user_a_cannot_modify_garage_b_mot_record(
    authenticated_user, second_vehicle, second_mot_record, session
):
    resp = authenticated_user.client.patch(
        f"/api/vehicles/{second_vehicle.id}/mot-records/{second_mot_record.id}",
        json={"result": "FAIL"},
    )

    assert resp.status_code == 404
    session.refresh(second_mot_record)
    assert second_mot_record.result != "FAIL"
