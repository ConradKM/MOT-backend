"""Browser (Twilio Voice SDK) outbound calling:

GET  /api/communications/voice/token
POST /api/communications/voice/outbound   (the TwiML App Voice URL)

Separate from the inbound ConversationRelay assistant - these tests never
touch app/ws/twilio_voice.py.
"""

import base64
import json

from twilio.request_validator import RequestValidator

from app.communications.voice_calling import client_identity, parse_client_identity
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)

WEBHOOK_BASE = "https://api.example.test"
AUTH_TOKEN = "test-auth-token"
VOICE_NUMBER = "+441611234567"


def _configure(app, monkeypatch, *, twiml_app="AP" + "0" * 32, api_key="SK" + "0" * 32):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setitem(app.config, "TWILIO_API_KEY_SID", api_key)
    monkeypatch.setitem(app.config, "TWILIO_API_KEY_SECRET", "sekret" if api_key else "")
    monkeypatch.setitem(app.config, "TWILIO_TWIML_APP_SID", twiml_app)
    monkeypatch.setitem(app.config, "TWILIO_WEBHOOK_VALIDATE", True)
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", WEBHOOK_BASE)


def _voice_number(session, garage, number=VOICE_NUMBER):
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number=number))
    session.commit()


def _signed(path, form):
    sig = RequestValidator(AUTH_TOKEN).compute_signature(f"{WEBHOOK_BASE}{path}", form)
    return {"X-Twilio-Signature": sig}


def _jwt_identity(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    grants = json.loads(base64.urlsafe_b64decode(payload))
    return str(grants["grants"]["identity"])


# --------------------------------------------------------------------------
# GET /voice/token
# --------------------------------------------------------------------------


def test_token_requires_authentication(client):
    assert client.get("/api/communications/voice/token").status_code == 401


def test_token_issued_for_an_authenticated_staff_member(
    app, session, garage, user, authenticated_client, monkeypatch
):
    _configure(app, monkeypatch)
    _voice_number(session, garage)

    resp = authenticated_client.get("/api/communications/voice/token")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["token"].count(".") == 2  # a JWT, not a raw secret
    assert body["caller_id"] == VOICE_NUMBER
    assert body["expires_in"] == 3600
    # Identity is scoped to this employee's own garage.
    assert body["identity"] == client_identity(garage.id, user.id)
    assert _jwt_identity(body["token"]) == body["identity"]
    # No secret leaks.
    assert AUTH_TOKEN not in resp.get_data(as_text=True)
    assert "sekret" not in resp.get_data(as_text=True)


def test_token_503_when_browser_calling_not_configured(
    app, session, garage, authenticated_client, monkeypatch
):
    _configure(app, monkeypatch, twiml_app="")  # no TwiML App
    _voice_number(session, garage)
    assert authenticated_client.get("/api/communications/voice/token").status_code == 503


def test_token_409_when_business_has_no_outbound_number(
    app, garage, authenticated_client, monkeypatch
):
    _configure(app, monkeypatch)  # configured, but no GarageCommunicationSettings row
    assert authenticated_client.get("/api/communications/voice/token").status_code == 409


def test_token_identity_is_tenant_specific(
    app, session, garage, second_garage, user, second_user, monkeypatch
):
    _configure(app, monkeypatch)
    a = client_identity(garage.id, user.id)
    b = client_identity(second_garage.id, second_user.id)
    assert a != b
    assert parse_client_identity(f"client:{a}") == (garage.id, user.id)
    assert parse_client_identity(f"client:{b}") == (second_garage.id, second_user.id)


# --------------------------------------------------------------------------
# POST /voice/outbound  (TwiML App Voice URL)
# --------------------------------------------------------------------------

_OUTBOUND = "/api/communications/voice/outbound"


def test_outbound_dials_from_the_business_number_and_logs_once(
    app, session, garage, user, client, monkeypatch
):
    _configure(app, monkeypatch)
    _voice_number(session, garage)
    form = {
        "From": f"client:{client_identity(garage.id, user.id)}",
        "To": "07123456789",
        "CallSid": "CAbrowser0001",
        "CallStatus": "ringing",
    }
    resp = client.post(_OUTBOUND, data=form, headers=_signed(_OUTBOUND, form))

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'callerId="{VOICE_NUMBER}"' in body  # never anything the client chose
    assert "<Number>+447123456789</Number>" in body

    rows = CommunicationLog.query.filter_by(garage_id=garage.id, channel="VOICE").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.direction == "OUTBOUND"
    assert row.external_id == "CAbrowser0001"
    assert row.call_sid == "CAbrowser0001"
    assert row.to_address == "+447123456789"
    assert row.from_address == VOICE_NUMBER
    assert row.initiated_by_employee_id == user.id
    assert row.external_provider == "twilio"


def test_outbound_reuses_the_row_on_a_repeat_webhook(
    app, session, garage, user, client, monkeypatch
):
    _configure(app, monkeypatch)
    _voice_number(session, garage)
    form = {
        "From": f"client:{client_identity(garage.id, user.id)}",
        "To": "07123456789",
        "CallSid": "CAbrowser0002",
    }
    for _ in range(3):
        client.post(_OUTBOUND, data=form, headers=_signed(_OUTBOUND, form))

    assert CommunicationLog.query.filter_by(external_id="CAbrowser0002").count() == 1


def test_outbound_rejects_a_bad_signature(app, session, garage, user, client, monkeypatch):
    _configure(app, monkeypatch)
    _voice_number(session, garage)
    form = {"From": f"client:{client_identity(garage.id, user.id)}", "To": "07123456789"}
    resp = client.post(_OUTBOUND, data=form, headers={"X-Twilio-Signature": "nope"})
    assert resp.status_code == 403
    assert CommunicationLog.query.count() == 0


def test_outbound_will_not_place_a_cross_tenant_call(
    app, session, garage, second_garage, user, second_user, client, monkeypatch
):
    _configure(app, monkeypatch)
    _voice_number(session, garage)
    # Forged identity: this garage's id, but an employee from another garage.
    forged = client_identity(garage.id, second_user.id)
    form = {"From": f"client:{forged}", "To": "07123456789", "CallSid": "CAx"}
    resp = client.post(_OUTBOUND, data=form, headers=_signed(_OUTBOUND, form))

    body = resp.get_data(as_text=True)
    assert "<Dial" not in body
    assert "could not be placed" in body
    assert CommunicationLog.query.count() == 0


def test_outbound_rejects_an_invalid_destination(app, session, garage, user, client, monkeypatch):
    _configure(app, monkeypatch)
    _voice_number(session, garage)
    form = {
        "From": f"client:{client_identity(garage.id, user.id)}",
        "To": "not-a-number",
        "CallSid": "CAy",
    }
    resp = client.post(_OUTBOUND, data=form, headers=_signed(_OUTBOUND, form))

    body = resp.get_data(as_text=True)
    assert "<Dial" not in body
    assert "not valid" in body
    assert CommunicationLog.query.count() == 0


def test_outbound_needs_a_configured_business_number(
    app, session, garage, user, client, monkeypatch
):
    _configure(app, monkeypatch)  # no _voice_number()
    form = {
        "From": f"client:{client_identity(garage.id, user.id)}",
        "To": "07123456789",
        "CallSid": "CAz",
    }
    resp = client.post(_OUTBOUND, data=form, headers=_signed(_OUTBOUND, form))

    assert "<Dial" not in resp.get_data(as_text=True)
    assert CommunicationLog.query.count() == 0


# --------------------------------------------------------------------------
# capabilities flag the frontend reads
# --------------------------------------------------------------------------


def test_overview_reports_outbound_calling_supported_once_configured(
    app, session, garage, user, authenticated_client, monkeypatch
):
    _configure(app, monkeypatch)
    _voice_number(session, garage)

    caps = authenticated_client.get("/api/communications/overview").get_json()["capabilities"]
    assert caps["outbound_calling_supported"] is True


def test_overview_outbound_calling_unsupported_without_a_twiml_app(
    app, session, garage, user, authenticated_client, monkeypatch
):
    _configure(app, monkeypatch, twiml_app="")
    _voice_number(session, garage)

    caps = authenticated_client.get("/api/communications/overview").get_json()["capabilities"]
    assert caps["outbound_calling_supported"] is False
