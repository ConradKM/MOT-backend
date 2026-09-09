"""API tests for customer sign-in: a booking reference + email, or email +
password once one has been set - plus /refresh and /set-password.
"""

import datetime

import pytest
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.booking_request import BookingRequest


@pytest.fixture()
def linked_booking_request(session, garage, customer):
    """A PENDING request already resolved to `customer` - what the public
    web form leaves behind at submission (app/public_booking/routes.py)."""
    br = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference="BK7F3K9Q2",
        customer_id=customer.id,
        customer_first_name=customer.first_name,
        customer_last_name=customer.last_name,
        customer_email=customer.email,
        customer_phone=customer.phone,
        vehicle_registration="AB12CDE",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=2),
    )
    session.add(br)
    session.commit()
    return br


def _reference_login(client, email, booking_reference):
    return client.post(
        "/api/customer/auth/login/reference",
        json={"email": email, "booking_reference": booking_reference},
    )


def _password_login(client, email, password):
    return client.post(
        "/api/customer/auth/login/password",
        json={"email": email, "password": password},
    )


# --------------------------------------------------------------------------
# Reference login
# --------------------------------------------------------------------------


def test_reference_login_succeeds(client, customer, linked_booking_request):
    resp = _reference_login(client, customer.email, linked_booking_request.booking_reference)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("access_token")
    assert body.get("refresh_token")


def test_reference_login_normalises_case_and_whitespace(client, customer, linked_booking_request):
    resp = _reference_login(client, customer.email, "  bk7f3k9q2  ")

    assert resp.status_code == 200


def test_reference_login_email_is_case_insensitive(client, customer, linked_booking_request):
    resp = _reference_login(
        client, customer.email.upper(), linked_booking_request.booking_reference
    )

    assert resp.status_code == 200


def test_reference_login_wrong_email_fails(client, linked_booking_request):
    resp = _reference_login(
        client, "someone-else@example.com", linked_booking_request.booking_reference
    )

    assert resp.status_code == 401


def test_reference_login_wrong_reference_fails(client, customer, linked_booking_request):
    resp = _reference_login(client, customer.email, "BK0000000")

    assert resp.status_code == 401


def test_reference_login_from_another_customer_fails(
    client, customer, second_customer, session, garage
):
    # A reference issued for a different customer's booking must not
    # authenticate someone who merely knows customer's email.
    other = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference="BK9999999",
        customer_id=second_customer.id,
        customer_first_name=second_customer.first_name,
        customer_last_name=second_customer.last_name,
        customer_email=second_customer.email,
        vehicle_registration="XY99ZZZ",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=2),
    )
    session.add(other)
    session.commit()

    resp = _reference_login(client, customer.email, other.booking_reference)

    assert resp.status_code == 401


def test_reference_login_missing_email_fails(client, linked_booking_request):
    resp = client.post(
        "/api/customer/auth/login/reference",
        json={"booking_reference": linked_booking_request.booking_reference},
    )
    assert resp.status_code == 422


def test_reference_login_missing_reference_fails(client, customer):
    resp = client.post("/api/customer/auth/login/reference", json={"email": customer.email})
    assert resp.status_code == 422


def test_reference_login_does_not_reveal_which_field_was_wrong(
    client, customer, linked_booking_request
):
    wrong_email = _reference_login(
        client, "nobody@example.com", linked_booking_request.booking_reference
    )
    wrong_ref = _reference_login(client, customer.email, "BK0000000")

    assert wrong_email.get_json()["message"] == wrong_ref.get_json()["message"]


def test_reference_login_token_is_accepted_by_the_customer_portal(
    client, customer, linked_booking_request
):
    token = _reference_login(
        client, customer.email, linked_booking_request.booking_reference
    ).get_json()["access_token"]

    resp = client.get("/api/customer/account", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["customer"]["id"] == str(customer.id)


# --------------------------------------------------------------------------
# Password login
# --------------------------------------------------------------------------


def test_password_login_succeeds(client, session, customer):
    customer.password_hash = generate_password_hash("correct-horse-battery-staple")
    session.commit()

    resp = _password_login(client, customer.email, "correct-horse-battery-staple")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("access_token")
    assert body.get("refresh_token")


def test_password_login_email_is_case_insensitive(client, session, customer):
    customer.password_hash = generate_password_hash("correct-horse-battery-staple")
    session.commit()

    resp = _password_login(client, customer.email.upper(), "correct-horse-battery-staple")

    assert resp.status_code == 200


def test_password_login_wrong_password_fails(client, session, customer):
    customer.password_hash = generate_password_hash("correct-horse-battery-staple")
    session.commit()

    resp = _password_login(client, customer.email, "wrong-password")

    assert resp.status_code == 401


def test_password_login_with_no_password_set_fails(client, customer):
    resp = _password_login(client, customer.email, "anything-at-all")

    assert resp.status_code == 401


def test_password_login_missing_fields_returns_422(client, customer):
    resp = client.post("/api/customer/auth/login/password", json={"email": customer.email})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Set password
# --------------------------------------------------------------------------


def test_set_password_requires_customer_authentication(client):
    resp = client.post("/api/customer/auth/set-password", json={"password": "a-long-password"})
    assert resp.status_code == 401


def test_set_password_rejects_an_employee_token(client, access_token):
    resp = client.post(
        "/api/customer/auth/set-password",
        json={"password": "a-long-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 401


def test_set_password_enables_password_login(customer_client, customer, session):
    resp = customer_client.post(
        "/api/customer/auth/set-password", json={"password": "a-brand-new-password"}
    )
    assert resp.status_code == 204

    db.session.refresh(customer)
    assert customer.password_hash is not None

    login = customer_client.post(
        "/api/customer/auth/login/password",
        json={"email": customer.email, "password": "a-brand-new-password"},
    )
    assert login.status_code == 200


def test_set_password_too_short_is_rejected(customer_client):
    resp = customer_client.post("/api/customer/auth/set-password", json={"password": "short"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Set password -> account-created email
#
# Through the real event bus (app/communications/events.py ->
# app/email/automation.py), not by calling the email service directly -
# rendering/dedupe/Resend-failure handling is covered by
# tests/test_email_service.py.
# --------------------------------------------------------------------------


def _capture_account_created(monkeypatch):
    box = {}
    monkeypatch.setattr(
        "app.email.automation.send_account_created_email",
        lambda customer: box.update(customer_id=str(customer.id), email=customer.email),
    )
    return box


def test_set_password_triggers_account_created_email(customer_client, customer, monkeypatch):
    box = _capture_account_created(monkeypatch)

    resp = customer_client.post(
        "/api/customer/auth/set-password", json={"password": "a-brand-new-password"}
    )

    assert resp.status_code == 204
    assert box["customer_id"] == str(customer.id)
    assert box["email"] == customer.email


def test_failed_set_password_does_not_trigger_email(customer_client, monkeypatch):
    box = _capture_account_created(monkeypatch)

    resp = customer_client.post("/api/customer/auth/set-password", json={"password": "short"})

    assert resp.status_code == 422
    assert box == {}


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def test_refresh_with_customer_refresh_token_returns_a_new_access_token(
    client, customer, linked_booking_request
):
    refresh_token = _reference_login(
        client, customer.email, linked_booking_request.booking_reference
    ).get_json()["refresh_token"]

    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert resp.status_code == 200
    assert resp.get_json().get("access_token")


def test_refresh_rejects_a_customer_access_token(client, customer, linked_booking_request):
    access_token = _reference_login(
        client, customer.email, linked_booking_request.booking_reference
    ).get_json()["access_token"]

    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert resp.status_code != 200


def test_refresh_rejects_an_employee_refresh_token(client, refresh_token):
    # refresh_token fixture is a garage employee's refresh token.
    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert resp.status_code == 401


def test_refresh_missing_token_fails(client):
    resp = client.post("/api/customer/auth/refresh")
    assert resp.status_code == 401
