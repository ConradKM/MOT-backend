"""Garage self-service password reset (owners + employees)."""

import datetime

from werkzeug.security import generate_password_hash

from app.auth.reset import _hash_token
from app.models.employee import Employee
from app.models.password_reset_token import PasswordResetToken

UTC = datetime.UTC
GENERIC = "If an account exists for this email address"


def _capture_token(monkeypatch):
    box = {}
    monkeypatch.setattr(
        "app.auth.routes.send_reset_link",
        lambda employee, raw: box.update(email=employee.email, token=raw),
    )
    return box


def _staff(session, garage, email="tech@garage-a.example", password="TechPass123!"):
    e = Employee(
        garage_id=garage.id,
        email=email,
        password_hash=generate_password_hash(password),
    )
    session.add(e)
    session.commit()
    return e


# --- forgot-password: no enumeration ------------------------------------


def test_forgot_password_unknown_email_is_generic(client):
    resp = client.post(
        "/api/auth/forgot-password", json={"email": "nobody@example.com"}
    )
    assert resp.status_code == 200
    assert GENERIC in resp.get_json()["message"]
    assert PasswordResetToken.query.count() == 0


def test_forgot_password_known_email_is_the_same_response(client, user, monkeypatch):
    box = _capture_token(monkeypatch)
    resp = client.post("/api/auth/forgot-password", json={"email": user.email})

    assert resp.status_code == 200
    assert GENERIC in resp.get_json()["message"]
    assert box["token"]
    assert PasswordResetToken.query.filter_by(employee_id=user.id).count() == 1


def test_forgot_password_works_for_an_employee_too(client, session, garage, monkeypatch):
    tech = _staff(session, garage)
    box = _capture_token(monkeypatch)

    resp = client.post("/api/auth/forgot-password", json={"email": tech.email})
    assert resp.status_code == 200
    assert box["token"]
    assert PasswordResetToken.query.filter_by(employee_id=tech.id).count() == 1


def test_forgot_password_ignores_a_deactivated_account(client, session, garage, monkeypatch):
    tech = _staff(session, garage)
    tech.is_active = False
    session.commit()
    box = _capture_token(monkeypatch)

    resp = client.post("/api/auth/forgot-password", json={"email": tech.email})
    assert resp.status_code == 200 and GENERIC in resp.get_json()["message"]
    assert "token" not in box
    assert PasswordResetToken.query.count() == 0


# --- reset-password ----------------------------------------------------


def test_reset_password_full_flow(client, user, monkeypatch):
    box = _capture_token(monkeypatch)
    client.post("/api/auth/forgot-password", json={"email": user.email})
    token = box["token"]

    # GET validates the token for the reset page.
    assert client.get(f"/api/auth/reset-password?token={token}").get_json()["valid"] is True

    resp = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "BrandNewPass456!"},
    )
    assert resp.status_code == 200
    assert "reset successfully" in resp.get_json()["message"]

    # New password works, old one doesn't.
    assert (
        client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "BrandNewPass456!"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "CorrectHorse123!"},
        ).status_code
        == 401
    )


def test_reset_token_is_single_use(client, user, monkeypatch):
    box = _capture_token(monkeypatch)
    client.post("/api/auth/forgot-password", json={"email": user.email})
    token = box["token"]

    client.post(
        "/api/auth/reset-password", json={"token": token, "password": "FirstReset123!"}
    )
    again = client.post(
        "/api/auth/reset-password", json={"token": token, "password": "SecondReset123!"}
    )
    assert again.status_code == 400
    assert "invalid or has expired" in again.get_json()["message"]


def test_expired_token_is_rejected(client, session, user):
    raw = "expired-raw-token-value"
    session.add(
        PasswordResetToken(
            employee_id=user.id,
            token_hash=_hash_token(raw),
            expires_at=datetime.datetime.now(UTC) - datetime.timedelta(minutes=1),
        )
    )
    session.commit()

    assert client.get(f"/api/auth/reset-password?token={raw}").status_code == 400
    resp = client.post(
        "/api/auth/reset-password", json={"token": raw, "password": "WontWork123!"}
    )
    assert resp.status_code == 400


def test_unknown_token_is_rejected(client, user):
    resp = client.post(
        "/api/auth/reset-password",
        json={"token": "never-issued", "password": "WontWork123!"},
    )
    assert resp.status_code == 400


def test_issuing_a_new_token_voids_the_previous_one(client, user, monkeypatch):
    box = _capture_token(monkeypatch)
    client.post("/api/auth/forgot-password", json={"email": user.email})
    first = box["token"]
    client.post("/api/auth/forgot-password", json={"email": user.email})
    second = box["token"]
    assert first != second

    assert (
        client.post(
            "/api/auth/reset-password",
            json={"token": first, "password": "OldLink123!"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/auth/reset-password",
            json={"token": second, "password": "NewLink123!"},
        ).status_code
        == 200
    )


def test_reset_is_per_user(client, session, garage, user, monkeypatch):
    tech = _staff(session, garage)
    owner_hash_before = user.password_hash
    box = _capture_token(monkeypatch)

    client.post("/api/auth/forgot-password", json={"email": tech.email})
    client.post(
        "/api/auth/reset-password",
        json={"token": box["token"], "password": "TechOnly123!"},
    )

    session.refresh(user)
    session.refresh(tech)
    assert user.password_hash == owner_hash_before
    assert (
        client.post(
            "/api/auth/login", json={"email": tech.email, "password": "TechOnly123!"}
        ).status_code
        == 200
    )


def test_reset_ends_existing_sessions(client, session, user, access_token, monkeypatch):
    # A live token works before the reset...
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    ).status_code == 200

    box = _capture_token(monkeypatch)
    client.post("/api/auth/forgot-password", json={"email": user.email})
    client.post(
        "/api/auth/reset-password",
        json={"token": box["token"], "password": "AfterReset123!"},
    )

    # ...and is rejected afterwards (issued before tokens_valid_from).
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    ).status_code in (401, 422)


def test_short_password_is_rejected(client, user, monkeypatch):
    box = _capture_token(monkeypatch)
    client.post("/api/auth/forgot-password", json={"email": user.email})
    resp = client.post(
        "/api/auth/reset-password", json={"token": box["token"], "password": "short"}
    )
    assert resp.status_code == 422
