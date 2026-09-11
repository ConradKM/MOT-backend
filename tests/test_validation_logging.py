"""Regression test for the Revive N Drive live-onboarding incident: a raw
marshmallow/webargs schema validation 422 (an out-of-range or unknown field -
no application-level ``abort(422, message=...)`` involved) left nothing in
the platform log stream beyond a bare access-log line with no body, making it
impossible to diagnose from Render logs alone. app/__init__.py::
_log_validation_errors fixes that.

Uses a public, unauthenticated endpoint (booking-request submission) with a
deliberately empty body, which 422s in schema validation before the view
function - and therefore before any database access - runs. This is the
narrowest possible reproduction of "a schema validation failure happened",
without needing a live Postgres the rest of the suite requires.
"""

import logging

from app import create_app
from app.config import TestConfig


def test_a_raw_schema_validation_422_is_logged_with_its_field_errors(caplog):
    app = create_app(TestConfig)
    client = app.test_client()

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/api/public/any-slug/booking-requests",
            json={},  # every required field omitted
        )

    assert resp.status_code == 422
    body = resp.get_json()
    assert "errors" in body  # the raw marshmallow shape - no top-level message

    validation_logs = [r for r in caplog.records if "[validation]" in r.message]
    assert len(validation_logs) == 1
    message = validation_logs[0].message
    assert "POST" in message
    assert "/api/public/any-slug/booking-requests" in message
    assert "422" in message
    # The actual rejected fields are in the log line - this is the whole
    # point: Render's access log alone never showed this.
    assert "customer_first_name" in message
    assert "customer_email" in message


def test_an_application_level_4xx_with_a_message_is_not_double_logged(caplog):
    """abort(422, message="...") bodies carry `message`, not `errors` - they
    are a deliberate business decision, already visible in the response, not
    a mystery this hook exists to explain. Nothing to prove them 422ing here
    without a DB-backed route, so this only proves the hook's own condition:
    a body with a message and no errors key is skipped."""
    app = create_app(TestConfig)

    with app.test_request_context():
        from flask import jsonify

        with caplog.at_level(logging.WARNING):
            response = jsonify({"code": 409, "status": "Conflict", "message": "Duplicate."})
            response.status_code = 409
            for func in app.after_request_funcs.get(None, []):
                response = func(response)

    assert not any("[validation]" in r.message for r in caplog.records)
