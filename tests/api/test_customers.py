"""API tests for /api/customers/ and /api/customers/{id}."""

import uuid

# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_create_customer(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/customers/",
        json={"first_name": "Alice", "last_name": "Smith", "email": "alice@example.com"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["first_name"] == "Alice"
    assert body["last_name"] == "Smith"
    assert body["email"] == "alice@example.com"
    assert "id" in body
    assert "created_at" in body
    assert "updated_at" in body


def test_create_customer_persists_internal_notes(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/customers/",
        json={
            "first_name": "Alice",
            "last_name": "Smith",
            "notes": "Prefers a call before any work starts.",
        },
    )

    assert resp.status_code == 201
    assert resp.get_json()["notes"] == "Prefers a call before any work starts."


def test_list_customers(authenticated_user, customer):
    resp = authenticated_user.client.get("/api/customers/")

    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, list)
    assert any(c["id"] == str(customer.id) for c in body)


def test_list_customers_search_filters_by_name(authenticated_user, session, garage):
    from app.models.customer import Customer

    session.add_all(
        [
            Customer(garage_id=garage.id, first_name="Zach", last_name="Zephyr"),
            Customer(garage_id=garage.id, first_name="Amy", last_name="Adams"),
        ]
    )
    session.commit()

    resp = authenticated_user.client.get("/api/customers/", query_string={"search": "zeph"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["first_name"] == "Zach"


def test_list_customers_searches_vehicle_registration(authenticated_user, session, garage):
    from app.models.customer import Customer
    from app.models.vehicle import Vehicle

    matching = Customer(garage_id=garage.id, first_name="Morgan", last_name="Driver")
    other = Customer(garage_id=garage.id, first_name="Jamie", last_name="Other")
    session.add_all([matching, other])
    session.flush()
    session.add_all(
        [
            Vehicle(
                garage_id=garage.id,
                customer_id=matching.id,
                registration_number="AB12 CDE",
            ),
            Vehicle(
                garage_id=garage.id,
                customer_id=other.id,
                registration_number="ZX99 XYZ",
            ),
        ]
    )
    session.commit()

    response = authenticated_user.client.get("/api/customers/", query_string={"search": "ab12 cde"})

    assert response.status_code == 200
    assert [customer["id"] for customer in response.get_json()] == [str(matching.id)]


def test_retrieve_customer(authenticated_user, customer):
    resp = authenticated_user.client.get(f"/api/customers/{customer.id}")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == str(customer.id)


def test_update_customer(authenticated_user, customer):
    resp = authenticated_user.client.patch(
        f"/api/customers/{customer.id}", json={"first_name": "Updated"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["first_name"] == "Updated"


def test_update_customer_notes_and_clear_them(authenticated_user, customer, session):
    note = "Keep keys at reception."
    updated = authenticated_user.client.patch(f"/api/customers/{customer.id}", json={"notes": note})

    assert updated.status_code == 200
    assert updated.get_json()["notes"] == note
    session.refresh(customer)
    assert customer.notes == note

    cleared = authenticated_user.client.patch(f"/api/customers/{customer.id}", json={"notes": None})

    assert cleared.status_code == 200
    assert cleared.get_json()["notes"] is None
    session.refresh(customer)
    assert customer.notes is None


def test_delete_customer_with_no_history_is_hard_deleted(authenticated_user, customer):
    resp = authenticated_user.client.delete(f"/api/customers/{customer.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {"archived": False, "deleted": True}

    get_resp = authenticated_user.client.get(f"/api/customers/{customer.id}")
    assert get_resp.status_code == 404


def test_delete_customer_with_a_vehicle_is_archived_not_deleted(
    authenticated_user, customer, vehicle, session
):
    resp = authenticated_user.client.delete(f"/api/customers/{customer.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {"archived": True, "deleted": False}

    # Still reachable by id - and so is the vehicle, unaffected.
    get_resp = authenticated_user.client.get(f"/api/customers/{customer.id}")
    assert get_resp.status_code == 200
    assert get_resp.get_json()["is_active"] is False

    session.refresh(vehicle)
    assert vehicle.customer_id == customer.id


def test_delete_customer_with_an_appointment_is_archived_not_deleted(
    authenticated_user, customer, make_appointment, session
):
    import datetime

    appt = make_appointment(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))

    resp = authenticated_user.client.delete(f"/api/customers/{customer.id}")

    assert resp.status_code == 200
    assert resp.get_json() == {"archived": True, "deleted": False}

    session.refresh(appt)
    assert appt.customer_id == customer.id


def test_archived_customer_is_excluded_from_the_default_list(authenticated_user, customer, vehicle):
    authenticated_user.client.delete(f"/api/customers/{customer.id}")

    listed = authenticated_user.client.get("/api/customers/").get_json()
    assert str(customer.id) not in {c["id"] for c in listed}

    listed_with_inactive = authenticated_user.client.get(
        "/api/customers/?include_inactive=true"
    ).get_json()
    assert str(customer.id) in {c["id"] for c in listed_with_inactive}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_create_customer_missing_required_fields(authenticated_user):
    resp = authenticated_user.client.post("/api/customers/", json={"first_name": "OnlyFirst"})

    assert resp.status_code == 422
    assert "last_name" in resp.get_json()["errors"]["json"]


def test_create_customer_invalid_email(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/customers/",
        json={"first_name": "A", "last_name": "B", "email": "not-an-email"},
    )

    assert resp.status_code == 422
    assert "email" in resp.get_json()["errors"]["json"]


def test_create_customer_invalid_data_types(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/customers/",
        json={"first_name": 12345, "last_name": True},
    )

    assert resp.status_code == 422


def test_customer_notes_have_a_reasonable_length_limit(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/customers/",
        json={"first_name": "A", "last_name": "B", "notes": "x" * 5001},
    )

    assert resp.status_code == 422
    assert "notes" in resp.get_json()["errors"]["json"]


def test_retrieve_customer_nonexistent_id_returns_404(authenticated_user):
    resp = authenticated_user.client.get(f"/api/customers/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.get_json()["message"] == "Customer not found"


def test_retrieve_customer_malformed_id_returns_404(authenticated_user):
    resp = authenticated_user.client.get("/api/customers/not-an-id")
    assert resp.status_code == 404


def test_create_customer_invalid_request_payload(authenticated_user):
    # Non-parseable JSON fails at Flask's request-parsing layer (400),
    # distinct from a well-formed-but-invalid payload (422).
    resp = authenticated_user.client.post(
        "/api/customers/",
        data="not-json",
        content_type="application/json",
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_list_customers_requires_auth(client):
    assert client.get("/api/customers/").status_code == 401


def test_create_customer_requires_auth(client):
    resp = client.post("/api/customers/", json={"first_name": "A", "last_name": "B"})
    assert resp.status_code == 401


def test_get_customer_requires_auth(client, customer):
    assert client.get(f"/api/customers/{customer.id}").status_code == 401


def test_update_customer_requires_auth(client, customer):
    resp = client.patch(f"/api/customers/{customer.id}", json={"first_name": "X"})
    assert resp.status_code == 401


def test_delete_customer_requires_auth(client, customer):
    assert client.delete(f"/api/customers/{customer.id}").status_code == 401


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_user_a_sees_only_garage_a_customers(
    authenticated_user, customer, second_authenticated_client, second_customer
):
    resp = authenticated_user.client.get("/api/customers/")
    ids = {c["id"] for c in resp.get_json()}

    assert str(customer.id) in ids
    assert str(second_customer.id) not in ids


def test_user_b_sees_only_garage_b_customers(
    second_authenticated_client, second_customer, authenticated_user, customer
):
    resp = second_authenticated_client.get("/api/customers/")
    ids = {c["id"] for c in resp.get_json()}

    assert str(second_customer.id) in ids
    assert str(customer.id) not in ids


def test_user_a_cannot_retrieve_garage_b_customer(authenticated_user, second_customer):
    resp = authenticated_user.client.get(f"/api/customers/{second_customer.id}")
    assert resp.status_code == 404


def test_user_a_cannot_modify_garage_b_customer(authenticated_user, second_customer, session):
    resp = authenticated_user.client.patch(
        f"/api/customers/{second_customer.id}", json={"first_name": "Hacked"}
    )

    assert resp.status_code == 404
    session.refresh(second_customer)
    assert second_customer.first_name != "Hacked"


def test_user_a_cannot_update_garage_b_customer_notes(authenticated_user, second_customer, session):
    resp = authenticated_user.client.patch(
        f"/api/customers/{second_customer.id}", json={"notes": "Private note"}
    )

    assert resp.status_code == 404
    session.refresh(second_customer)
    assert second_customer.notes is None


def test_user_a_cannot_delete_garage_b_customer(authenticated_user, second_customer, session):
    resp = authenticated_user.client.delete(f"/api/customers/{second_customer.id}")

    assert resp.status_code == 404
    assert session.get(type(second_customer), second_customer.id) is not None
