"""API tests for the Twilio webhook endpoints:

POST /api/webhooks/twilio/voice/incoming
POST /api/webhooks/twilio/voice/status
POST /api/webhooks/twilio/whatsapp/incoming
POST /api/webhooks/twilio/whatsapp/status

Also covers the one thing every other test file already proves implicitly
(none of them configure Twilio, and they all pass): booking still works with
communications disabled. This file makes that explicit.
"""

import datetime

from twilio.request_validator import RequestValidator

from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.customer import Customer

WEBHOOK_BASE = "https://api.example.test"
AUTH_TOKEN = "test-auth-token"


def _configure_twilio(app, monkeypatch, *, validate=True):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setitem(app.config, "TWILIO_WEBHOOK_VALIDATE", validate)
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", WEBHOOK_BASE)


def _signed_headers(path, form):
    validator = RequestValidator(AUTH_TOKEN)
    signature = validator.compute_signature(f"{WEBHOOK_BASE}{path}", form)
    return {"X-Twilio-Signature": signature}


# --------------------------------------------------------------------------
# Booking still works with communications disabled
# --------------------------------------------------------------------------


def test_booking_flow_works_with_communications_disabled(client, garage):
    """No TWILIO_* env vars set anywhere in this suite (see TestConfig) - this
    just makes that guarantee explicit for the public booking endpoint, which
    now emits BOOKING_REQUEST_CREATED on every successful submission."""
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json={
            "customer_first_name": "Alex",
            "customer_last_name": "Turner",
            "customer_email": "alex.turner@example.com",
            "customer_phone": "07123 456789",
            "vehicle_registration": "PB11 REQ",
            "preferred_date": (datetime.date.today() + datetime.timedelta(days=30)).isoformat(),
        },
    )
    assert resp.status_code == 201


# --------------------------------------------------------------------------
# Voice: /incoming
# --------------------------------------------------------------------------


def test_voice_incoming_returns_503_when_twilio_not_configured(client, garage):
    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": "+441111111111", "From": "+447700900000", "CallSid": "CA1"},
    )
    assert resp.status_code == 503


def test_voice_incoming_rejects_invalid_signature(app, client, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": "+441111111111", "From": "+447700900000", "CallSid": "CA1"},
        headers={"X-Twilio-Signature": "not-a-real-signature"},
    )
    assert resp.status_code == 403


def test_voice_incoming_resolves_tenant_greets_by_garage_name_and_logs_call(
    app, session, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111"))
    session.commit()

    form = {"To": "+441111111111", "From": "+447700900000", "CallSid": "CA-known-1"}
    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data=form,
        headers=_signed_headers("/api/webhooks/twilio/voice/incoming", form),
    )

    assert resp.status_code == 200
    assert garage.name.encode() in resp.data

    log = CommunicationLog.query.filter_by(external_id="CA-known-1").one()
    assert log.garage_id == garage.id
    assert log.channel == "VOICE"
    assert log.direction == "INBOUND"
    assert log.from_address == "+447700900000"


def test_voice_incoming_unknown_number_answers_safely_without_logging(
    app, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)

    form = {"To": "+449999999999", "From": "+447700900000", "CallSid": "CA-unknown-1"}
    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data=form,
        headers=_signed_headers("/api/webhooks/twilio/voice/incoming", form),
    )

    assert resp.status_code == 200
    assert b"not currently in service" in resp.data
    assert CommunicationLog.query.filter_by(external_id="CA-unknown-1").first() is None


def test_voice_incoming_cross_tenant_isolation(
    app, session, client, garage, second_garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111"))
    session.add(
        GarageCommunicationSettings(garage_id=second_garage.id, voice_phone_number="+442222222222")
    )
    session.commit()

    form = {"To": "+442222222222", "From": "+447700900000", "CallSid": "CA-tenant-b"}
    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data=form,
        headers=_signed_headers("/api/webhooks/twilio/voice/incoming", form),
    )

    assert resp.status_code == 200
    assert second_garage.name.encode() in resp.data
    assert garage.name.encode() not in resp.data

    log = CommunicationLog.query.filter_by(external_id="CA-tenant-b").one()
    assert log.garage_id == second_garage.id


# --------------------------------------------------------------------------
# Voice: /status
# --------------------------------------------------------------------------


def test_voice_status_updates_the_matching_log_idempotently(
    app, session, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(
        CommunicationLog(
            garage_id=garage.id, channel="VOICE", direction="OUTBOUND",
            status="queued", external_id="CA-status-1",
        )
    )
    session.commit()

    path = "/api/webhooks/twilio/voice/status"
    for call_status, duration in (("ringing", None), ("completed", "37")):
        form = {"CallSid": "CA-status-1", "CallStatus": call_status}
        if duration is not None:
            form["CallDuration"] = duration
        resp = client.post(path, data=form, headers=_signed_headers(path, form))
        assert resp.status_code == 204

    matches = CommunicationLog.query.filter_by(external_id="CA-status-1").all()
    assert len(matches) == 1
    assert matches[0].status == "completed"
    assert matches[0].call_duration_seconds == 37


# --------------------------------------------------------------------------
# WhatsApp: /incoming
# --------------------------------------------------------------------------


def test_whatsapp_incoming_resolves_tenant_and_matches_customer(
    app, session, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, whatsapp_sender="whatsapp:+14155238886")
    )
    known_customer = Customer(
        garage_id=garage.id, first_name="Sam", last_name="Ridley",
        email="sam.ridley@example.com", phone="+447123456789",
    )
    session.add(known_customer)
    session.commit()

    path = "/api/webhooks/twilio/whatsapp/incoming"
    form = {
        "To": "whatsapp:+14155238886",
        "From": "whatsapp:+447123456789",
        "MessageSid": "SM-known-1",
        "Body": "Can I book an MOT?",
    }
    resp = client.post(path, data=form, headers=_signed_headers(path, form))

    assert resp.status_code == 200

    log = CommunicationLog.query.filter_by(external_id="SM-known-1").one()
    assert log.garage_id == garage.id
    assert log.channel == "WHATSAPP"
    assert log.direction == "INBOUND"
    assert log.body == "Can I book an MOT?"
    assert log.customer_id == known_customer.id


def test_whatsapp_incoming_unknown_sender_answers_safely_without_logging(
    app, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)

    path = "/api/webhooks/twilio/whatsapp/incoming"
    form = {
        "To": "whatsapp:+19999999999",
        "From": "whatsapp:+447123456789",
        "MessageSid": "SM-unknown-1",
        "Body": "hello?",
    }
    resp = client.post(path, data=form, headers=_signed_headers(path, form))

    assert resp.status_code == 200
    assert CommunicationLog.query.filter_by(external_id="SM-unknown-1").first() is None


def test_whatsapp_incoming_cross_tenant_isolation(
    app, session, client, garage, second_garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, whatsapp_sender="whatsapp:+10000000001")
    )
    session.add(
        GarageCommunicationSettings(
            garage_id=second_garage.id, whatsapp_sender="whatsapp:+10000000002"
        )
    )
    session.commit()

    path = "/api/webhooks/twilio/whatsapp/incoming"
    form = {
        "To": "whatsapp:+10000000002",
        "From": "whatsapp:+447123456789",
        "MessageSid": "SM-tenant-b",
        "Body": "hi",
    }
    resp = client.post(path, data=form, headers=_signed_headers(path, form))

    assert resp.status_code == 200
    log = CommunicationLog.query.filter_by(external_id="SM-tenant-b").one()
    assert log.garage_id == second_garage.id


def test_whatsapp_incoming_no_auto_ack_by_default(app, session, client, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, whatsapp_sender="whatsapp:+14155238886")
    )
    session.commit()

    path = "/api/webhooks/twilio/whatsapp/incoming"
    form = {
        "To": "whatsapp:+14155238886", "From": "whatsapp:+447123456789",
        "MessageSid": "SM-no-ack", "Body": "hi",
    }
    resp = client.post(path, data=form, headers=_signed_headers(path, form))

    assert resp.status_code == 200
    assert b"<Message>" not in resp.data


def test_whatsapp_incoming_auto_ack_when_explicitly_enabled(
    app, session, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "TWILIO_WHATSAPP_AUTO_ACK", True)
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, whatsapp_sender="whatsapp:+14155238886")
    )
    session.commit()

    path = "/api/webhooks/twilio/whatsapp/incoming"
    form = {
        "To": "whatsapp:+14155238886", "From": "whatsapp:+447123456789",
        "MessageSid": "SM-with-ack", "Body": "hi",
    }
    resp = client.post(path, data=form, headers=_signed_headers(path, form))

    assert resp.status_code == 200
    assert b"<Message>" in resp.data
    assert garage.name.encode() in resp.data


# --------------------------------------------------------------------------
# WhatsApp: /status
# --------------------------------------------------------------------------


def test_whatsapp_status_updates_the_matching_log_idempotently(
    app, session, client, garage, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    session.add(
        CommunicationLog(
            garage_id=garage.id, channel="WHATSAPP", direction="OUTBOUND",
            status="queued", external_id="SM-status-1",
        )
    )
    session.commit()

    path = "/api/webhooks/twilio/whatsapp/status"
    for message_status in ("sent", "delivered"):
        form = {"MessageSid": "SM-status-1", "MessageStatus": message_status}
        resp = client.post(path, data=form, headers=_signed_headers(path, form))
        assert resp.status_code == 204

    matches = CommunicationLog.query.filter_by(external_id="SM-status-1").all()
    assert len(matches) == 1
    assert matches[0].status == "delivered"
