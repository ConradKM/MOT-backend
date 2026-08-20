"""API tests for /api/customers/ and /api/customers/{id}."""


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


def test_list_customers(authenticated_user, customer):
    resp = authenticated_user.client.get("/api/customers/")

    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, list)
    assert any(c["id"] == customer.id for c in body)


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


def test_retrieve_customer(authenticated_user, customer):
    resp = authenticated_user.client.get(f"/api/customers/{customer.id}")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == customer.id


def test_update_customer(authenticated_user, customer):
    resp = authenticated_user.client.patch(
        f"/api/customers/{customer.id}", json={"first_name": "Updated"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["first_name"] == "Updated"


def test_delete_customer(authenticated_user, customer):
    resp = authenticated_user.client.delete(f"/api/customers/{customer.id}")

    assert resp.status_code == 204
    assert resp.data == b""

    get_resp = authenticated_user.client.get(f"/api/customers/{customer.id}")
    assert get_resp.status_code == 404


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


def test_retrieve_customer_invalid_id_returns_404(authenticated_user):
    resp = authenticated_user.client.get("/api/customers/999999")
    assert resp.status_code == 404


def test_retrieve_customer_non_integer_id_returns_404(authenticated_user):
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
    resp = client.post(
        "/api/customers/", json={"first_name": "A", "last_name": "B"}
    )
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

    assert customer.id in ids
    assert second_customer.id not in ids


def test_user_b_sees_only_garage_b_customers(
    second_authenticated_client, second_customer, authenticated_user, customer
):
    resp = second_authenticated_client.get("/api/customers/")
    ids = {c["id"] for c in resp.get_json()}

    assert second_customer.id in ids
    assert customer.id not in ids


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


def test_user_a_cannot_delete_garage_b_customer(authenticated_user, second_customer, session):
    resp = authenticated_user.client.delete(f"/api/customers/{second_customer.id}")

    assert resp.status_code == 404
    assert session.get(type(second_customer), second_customer.id) is not None
