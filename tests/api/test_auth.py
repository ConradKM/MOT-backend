"""API tests for POST /api/auth/register, /api/auth/login, /api/auth/refresh."""

from app.models.employee import Employee
from app.models.garage import Garage

VALID_PAYLOAD = {
    "garage_name": "New Garage",
    "email": "new-owner@example.com",
    "password": "correct-password-1",
}


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_register_valid_registration_succeeds(client):
    resp = client.post("/api/auth/register", json=VALID_PAYLOAD)
    assert resp.status_code == 201


def test_register_returns_access_and_refresh_tokens(client):
    resp = client.post("/api/auth/register", json=VALID_PAYLOAD)
    body = resp.get_json()

    assert body.get("access_token")
    assert body.get("refresh_token")


def test_register_creates_a_garage(client, session):
    client.post("/api/auth/register", json=VALID_PAYLOAD)

    garage = Garage.query.filter_by(name="New Garage").first()
    assert garage is not None


def test_register_creates_a_user(client, session):
    client.post("/api/auth/register", json=VALID_PAYLOAD)

    user = Employee.query.filter_by(email=VALID_PAYLOAD["email"]).first()
    assert user is not None


def test_register_user_belongs_to_the_newly_created_garage(client, session):
    client.post("/api/auth/register", json=VALID_PAYLOAD)

    user = Employee.query.filter_by(email=VALID_PAYLOAD["email"]).first()
    garage = Garage.query.filter_by(name="New Garage").first()

    assert user.garage_id == garage.id


def test_register_password_is_hashed(client, session):
    client.post("/api/auth/register", json=VALID_PAYLOAD)

    user = Employee.query.filter_by(email=VALID_PAYLOAD["email"]).first()

    assert user.password_hash != VALID_PAYLOAD["password"]
    assert VALID_PAYLOAD["password"] not in user.password_hash


def test_register_missing_email_fails(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "email"}
    resp = client.post("/api/auth/register", json=payload)

    assert resp.status_code == 422
    assert "email" in resp.get_json()["errors"]["json"]


def test_register_missing_password_fails(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "password"}
    resp = client.post("/api/auth/register", json=payload)

    assert resp.status_code == 422
    assert "password" in resp.get_json()["errors"]["json"]


def test_register_missing_garage_name_fails(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "garage_name"}
    resp = client.post("/api/auth/register", json=payload)

    assert resp.status_code == 422
    assert "garage_name" in resp.get_json()["errors"]["json"]


def test_register_invalid_email_fails(client):
    payload = {**VALID_PAYLOAD, "email": "not-an-email"}
    resp = client.post("/api/auth/register", json=payload)

    assert resp.status_code == 422
    assert "email" in resp.get_json()["errors"]["json"]


def test_register_duplicate_email_fails(client):
    client.post("/api/auth/register", json=VALID_PAYLOAD)

    resp = client.post(
        "/api/auth/register",
        json={**VALID_PAYLOAD, "garage_name": "Another Garage"},
    )

    assert resp.status_code == 409


def test_register_invalid_request_body_fails(client):
    # Non-parseable JSON fails at Flask's request-parsing layer (400), which
    # is distinct from a well-formed-but-invalid payload (422, see the
    # marshmallow validation tests above).
    resp = client.post(
        "/api/auth/register",
        data="not-json",
        content_type="application/json",
    )

    assert resp.status_code == 400


def test_register_does_not_leak_password_in_response(client):
    resp = client.post("/api/auth/register", json=VALID_PAYLOAD)
    body = resp.get_json()

    assert "password" not in body
    assert "password_hash" not in body


def test_register_generates_a_slug_from_the_garage_name(client, session):
    client.post(
        "/api/auth/register",
        json={**VALID_PAYLOAD, "garage_name": "Bob's Tyres & Exhausts"},
    )

    garage = Garage.query.filter_by(name="Bob's Tyres & Exhausts").first()
    assert garage.slug == "bob-s-tyres-exhausts"


def test_register_gives_same_named_garages_distinct_slugs(client, session):
    client.post(
        "/api/auth/register",
        json={"garage_name": "City Motors", "email": "a@example.com", "password": "password-1a"},
    )
    client.post(
        "/api/auth/register",
        json={"garage_name": "City Motors", "email": "b@example.com", "password": "password-1b"},
    )

    slugs = sorted(g.slug for g in Garage.query.filter_by(name="City Motors").all())
    assert slugs == ["city-motors", "city-motors-2"]


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_login_correct_credentials_succeed(client, user):
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "CorrectHorse123!"},
    )
    assert resp.status_code == 200


def test_login_returns_access_and_refresh_tokens(client, user):
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "CorrectHorse123!"},
    )
    body = resp.get_json()

    assert body.get("access_token")
    assert body.get("refresh_token")


def test_login_incorrect_password_fails(client, user):
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )
    assert resp.status_code == 401


def test_login_unknown_email_fails(client):
    resp = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "whatever123"},
    )
    assert resp.status_code == 401


def test_login_missing_email_fails(client):
    resp = client.post("/api/auth/login", json={"password": "whatever123"})
    assert resp.status_code == 422


def test_login_missing_password_fails(client, user):
    resp = client.post("/api/auth/login", json={"email": user.email})
    assert resp.status_code == 422


def test_login_missing_credentials_fails(client):
    resp = client.post("/api/auth/login", json={})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def test_refresh_valid_refresh_token_generates_new_access_token(client, refresh_token):
    resp = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert resp.status_code == 200
    assert "access_token" in resp.get_json()


def test_refresh_missing_token_fails(client):
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_refresh_invalid_token_fails(client):
    resp = client.post(
        "/api/auth/refresh",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code in (401, 422)


def test_refresh_rejects_an_access_token(client, access_token):
    resp = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert resp.status_code != 200
