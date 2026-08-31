"""API tests for /api/appointment-types/ and /api/appointment-types/{id}."""


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_owner_can_create_appointment_type(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/",
        json={"name": "Brake Repair", "description": "Full brake job", "base_price": "149.99"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Brake Repair"
    assert body["description"] == "Full brake job"
    assert body["base_price"] == "149.99"
    assert body["garage_id"] == str(authenticated_user.garage.id)


def test_create_appointment_type_description_and_price_are_optional(authenticated_user):
    resp = authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["description"] is None
    assert body["base_price"] is None
    assert body["default_duration_minutes"] is None


def test_create_appointment_type_with_default_duration(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "default_duration_minutes": 45}
    )

    assert resp.status_code == 201
    assert resp.get_json()["default_duration_minutes"] == 45


def test_owner_can_update_default_duration(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"default_duration_minutes": 30}
    )

    assert resp.status_code == 200
    assert resp.get_json()["default_duration_minutes"] == 30


def test_create_appointment_type_invalid_duration(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "default_duration_minutes": 0}
    )
    assert resp.status_code == 422
    assert "default_duration_minutes" in resp.get_json()["errors"]["json"]


def test_create_appointment_type_defaults_to_active_status(authenticated_user):
    resp = authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})
    assert resp.get_json()["status"] == "ACTIVE"


def test_create_appointment_type_with_explicit_status(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Old Service", "status": "DEPRECATED"}
    )
    assert resp.status_code == 201
    assert resp.get_json()["status"] == "DEPRECATED"


def test_owner_can_hide_an_appointment_type(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"status": "HIDDEN"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "HIDDEN"


def test_owner_can_deprecate_an_appointment_type(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"status": "DEPRECATED"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "DEPRECATED"


def test_create_appointment_type_invalid_status(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "status": "ARCHIVED"}
    )
    assert resp.status_code == 422
    assert "status" in resp.get_json()["errors"]["json"]


def test_list_filtered_by_status_excludes_others(authenticated_user):
    active = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    hidden = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Old MOT", "status": "HIDDEN"}
    ).get_json()

    resp = authenticated_user.client.get("/api/appointment-types/", query_string={"status": "ACTIVE"})

    ids = {t["id"] for t in resp.get_json()}
    assert active["id"] in ids
    assert hidden["id"] not in ids


def test_list_without_filter_includes_hidden_and_deprecated(authenticated_user):
    authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})
    hidden = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Old MOT", "status": "HIDDEN"}
    ).get_json()
    deprecated = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Ancient MOT", "status": "DEPRECATED"}
    ).get_json()

    resp = authenticated_user.client.get("/api/appointment-types/")

    ids = {t["id"] for t in resp.get_json()}
    assert hidden["id"] in ids
    assert deprecated["id"] in ids


def test_cannot_book_new_appointment_against_a_hidden_type(authenticated_user, customer):
    hidden = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Old MOT", "status": "HIDDEN"}
    ).get_json()

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": hidden["id"],
        },
    )
    assert resp.status_code == 422


def test_cannot_reassign_existing_appointment_to_a_deprecated_type(authenticated_user, customer):
    active = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    deprecated = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Ancient MOT", "status": "DEPRECATED"}
    ).get_json()
    appointment = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": active["id"],
        },
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}",
        json={"appointment_type_id": deprecated["id"]},
    )
    assert resp.status_code == 422


def test_hiding_a_type_does_not_affect_appointments_already_using_it(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    appointment = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    ).get_json()

    authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type['id']}", json={"status": "HIDDEN"}
    )

    # Editing an unrelated field on the existing appointment still works -
    # it doesn't get invalidated by its type being hidden afterwards.
    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}", json={"notes": "Customer called to confirm"}
    )
    assert resp.status_code == 200

    get_resp = authenticated_user.client.get(f"/api/appointments/{appointment['id']}")
    assert get_resp.get_json()["appointment_type_id"] == appt_type["id"]


def test_list_appointment_types(authenticated_user):
    authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})
    authenticated_user.client.post("/api/appointment-types/", json={"name": "Service"})

    resp = authenticated_user.client.get("/api/appointment-types/")

    assert resp.status_code == 200
    names = {t["name"] for t in resp.get_json()}
    assert {"MOT", "Service"} <= names


def test_retrieve_appointment_type(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.get(f"/api/appointment-types/{created['id']}")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == created["id"]


def test_owner_can_update_appointment_type(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"base_price": "54.85"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["base_price"] == "54.85"


def test_owner_can_delete_unused_appointment_type(authenticated_user):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    resp = authenticated_user.client.delete(f"/api/appointment-types/{created['id']}")
    assert resp.status_code == 204

    get_resp = authenticated_user.client.get(f"/api/appointment-types/{created['id']}")
    assert get_resp.status_code == 404


# --------------------------------------------------------------------------
# Default duration -> end_time derivation
# --------------------------------------------------------------------------


def test_omitting_end_time_derives_it_from_default_duration(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "default_duration_minutes": 45}
    ).get_json()

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["start_time"] == "2026-09-15T08:00:00+00:00"
    assert body["end_time"] == "2026-09-15T08:45:00+00:00"


def test_omitting_end_time_without_a_default_duration_fails(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "Repair"}
    ).get_json()

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    )

    assert resp.status_code == 422


def test_explicit_end_time_overrides_default_duration(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT", "default_duration_minutes": 45}
    ).get_json()

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T11:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    )

    assert resp.status_code == 201
    assert resp.get_json()["end_time"] == "2026-09-15T10:00:00+00:00"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_create_appointment_type_missing_name(authenticated_user):
    resp = authenticated_user.client.post("/api/appointment-types/", json={})

    assert resp.status_code == 422
    assert "name" in resp.get_json()["errors"]["json"]


def test_retrieve_appointment_type_nonexistent_id_returns_404(authenticated_user):
    import uuid

    resp = authenticated_user.client.get(f"/api/appointment-types/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_cannot_delete_appointment_type_in_use(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    )

    resp = authenticated_user.client.delete(f"/api/appointment-types/{appt_type['id']}")
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# Authorization / authentication
# --------------------------------------------------------------------------


def test_create_appointment_type_requires_auth(client):
    resp = client.post("/api/appointment-types/", json={"name": "MOT"})
    assert resp.status_code == 401


def test_staff_cannot_create_appointment_type(authenticated_user, session):
    authenticated_user.user.role = "STAFF"
    session.commit()

    resp = authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})
    assert resp.status_code == 403


def test_staff_can_list_appointment_types(authenticated_user, session):
    authenticated_user.client.post("/api/appointment-types/", json={"name": "MOT"})

    authenticated_user.user.role = "STAFF"
    session.commit()

    resp = authenticated_user.client.get("/api/appointment-types/")
    assert resp.status_code == 200


def test_staff_cannot_update_appointment_type(authenticated_user, session):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    authenticated_user.user.role = "STAFF"
    session.commit()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"name": "Changed"}
    )
    assert resp.status_code == 403


def test_staff_cannot_delete_appointment_type(authenticated_user, session):
    created = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    authenticated_user.user.role = "STAFF"
    session.commit()

    resp = authenticated_user.client.delete(f"/api/appointment-types/{created['id']}")
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_garage_a_only_sees_its_own_appointment_types(
    authenticated_user, second_authenticated_client
):
    authenticated_user.client.post("/api/appointment-types/", json={"name": "Garage A Type"})
    second_authenticated_client.post("/api/appointment-types/", json={"name": "Garage B Type"})

    resp_a = authenticated_user.client.get("/api/appointment-types/")
    resp_b = second_authenticated_client.get("/api/appointment-types/")

    names_a = {t["name"] for t in resp_a.get_json()}
    names_b = {t["name"] for t in resp_b.get_json()}

    assert "Garage A Type" in names_a and "Garage B Type" not in names_a
    assert "Garage B Type" in names_b and "Garage A Type" not in names_b


def test_user_a_cannot_retrieve_garage_b_appointment_type(
    authenticated_user, second_authenticated_client
):
    created = second_authenticated_client.post(
        "/api/appointment-types/", json={"name": "Garage B Type"}
    ).get_json()

    resp = authenticated_user.client.get(f"/api/appointment-types/{created['id']}")
    assert resp.status_code == 404


def test_user_a_cannot_modify_garage_b_appointment_type(
    authenticated_user, second_authenticated_client
):
    created = second_authenticated_client.post(
        "/api/appointment-types/", json={"name": "Garage B Type"}
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{created['id']}", json={"name": "Hacked"}
    )
    assert resp.status_code == 404


def test_user_a_cannot_delete_garage_b_appointment_type(
    authenticated_user, second_authenticated_client
):
    created = second_authenticated_client.post(
        "/api/appointment-types/", json={"name": "Garage B Type"}
    ).get_json()

    resp = authenticated_user.client.delete(f"/api/appointment-types/{created['id']}")
    assert resp.status_code == 404
