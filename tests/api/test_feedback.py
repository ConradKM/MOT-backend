"""Business Dashboard Help Centre: POST /api/feedback.

Covers the DB-is-source-of-truth requirement (a row is created and `done`
always defaults to False, regardless of what the client sends), tenant
isolation (garage_id/employee_id/business_name are always derived from the
authenticated employee, never the request body), and that a failing support
notification email never loses an already-saved feedback row.
"""

from app.models.feedback import Feedback


def _payload(**overrides):
    payload = {
        "type": "BUG",
        "subject": "Booking calendar not loading",
        "message": "The booking calendar doesn't load when I try to open it.",
        "priority": "HIGH",
    }
    payload.update(overrides)
    return payload


def test_create_feedback_persists_row_scoped_to_authenticated_business(authenticated_user, session):
    resp = authenticated_user.client.post("/api/feedback/", json=_payload())

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["business_name"] == authenticated_user.garage.name
    assert body["type"] == "BUG"
    assert body["priority"] == "HIGH"
    assert body["done"] is False

    row = session.get(Feedback, body["id"])
    assert row is not None
    assert row.garage_id == authenticated_user.garage.id
    assert row.employee_id == authenticated_user.user.id
    assert row.business_name == authenticated_user.garage.name
    assert row.done is False


def test_create_feedback_defaults_contact_email_to_the_signed_in_employee(authenticated_user):
    resp = authenticated_user.client.post("/api/feedback/", json=_payload())

    assert resp.status_code == 201
    assert resp.get_json()["user_email"] == authenticated_user.user.email


def test_create_feedback_allows_overriding_contact_email(authenticated_user):
    resp = authenticated_user.client.post(
        "/api/feedback/", json=_payload(email="someone-else@example.com")
    )

    assert resp.status_code == 201
    assert resp.get_json()["user_email"] == "someone-else@example.com"


def test_create_feedback_requires_a_non_empty_message(authenticated_user, session):
    resp = authenticated_user.client.post("/api/feedback/", json=_payload(message=""))

    assert resp.status_code == 422
    assert session.query(Feedback).count() == 0


def test_create_feedback_rejects_an_unknown_feedback_type(authenticated_user):
    resp = authenticated_user.client.post("/api/feedback/", json=_payload(type="NOT_A_TYPE"))

    assert resp.status_code == 422


def test_create_feedback_ignores_client_supplied_identity_fields(authenticated_user, session):
    """business_id/user_id/done are not schema fields at all, so a client
    trying to inject them either has no effect or is rejected outright - it
    can never reach the row."""
    resp = authenticated_user.client.post(
        "/api/feedback/",
        json=_payload(garage_id="11111111-1111-1111-1111-111111111111", done=True),
    )

    # Either rejected as an unknown field, or silently ignored - never honoured.
    if resp.status_code == 201:
        row = session.get(Feedback, resp.get_json()["id"])
        assert row.garage_id == authenticated_user.garage.id
        assert row.done is False
    else:
        assert resp.status_code == 422
        assert session.query(Feedback).count() == 0


def test_create_feedback_survives_a_notification_email_failure(
    authenticated_user, session, monkeypatch
):
    def _boom(**_kwargs):
        raise RuntimeError("Resend is down")

    monkeypatch.setattr("app.feedback.service.send_email", _boom)

    resp = authenticated_user.client.post("/api/feedback/", json=_payload())

    assert resp.status_code == 201
    assert session.query(Feedback).count() == 1


def test_create_feedback_requires_authentication(client):
    resp = client.post("/api/feedback/", json=_payload())

    assert resp.status_code == 401
