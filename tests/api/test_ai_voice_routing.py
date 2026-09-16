"""POST /api/webhooks/twilio/voice/incoming's OpenAI-Realtime routing branch
(app/communications/voice_webhooks.py) - takes priority over
ConversationRelay/static when both OPENAI_VOICE_ENABLED and OPENAI_API_KEY
are set, and falls through safely otherwise.
"""

from twilio.request_validator import RequestValidator

from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)

WEBHOOK_BASE = "https://api.example.test"
AUTH_TOKEN = "test-auth-token"


def _configure_twilio(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setitem(app.config, "TWILIO_WEBHOOK_VALIDATE", True)
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", WEBHOOK_BASE)


def _signed_headers(path, form):
    validator = RequestValidator(AUTH_TOKEN)
    signature = validator.compute_signature(f"{WEBHOOK_BASE}{path}", form)
    return {"X-Twilio-Signature": signature}


def _incoming(client, form):
    path = "/api/webhooks/twilio/voice/incoming"
    return client.post(path, data=form, headers=_signed_headers(path, form))


def test_openai_voice_used_when_enabled_and_configured(app, session, client, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", True)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111")
    )
    session.commit()

    resp = _incoming(client, {"To": "+441111111111", "From": "+447700900000", "CallSid": "CA-ai-1"})

    assert resp.status_code == 200
    assert b"<Connect>" in resp.data
    assert b"api/ws/twilio/openai-voice" in resp.data
    assert str(garage.id).encode() in resp.data


def test_openai_voice_not_used_when_flag_off(app, session, client, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", False)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111")
    )
    session.commit()

    resp = _incoming(client, {"To": "+441111111111", "From": "+447700900000", "CallSid": "CA-ai-2"})

    assert resp.status_code == 200
    assert b"api/ws/twilio/openai-voice" not in resp.data


def test_openai_voice_not_used_without_an_api_key(app, session, client, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", True)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "")
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111")
    )
    session.commit()

    resp = _incoming(client, {"To": "+441111111111", "From": "+447700900000", "CallSid": "CA-ai-3"})

    assert resp.status_code == 200
    assert b"api/ws/twilio/openai-voice" not in resp.data


def test_openai_voice_build_failure_falls_back_safely(app, session, client, garage, monkeypatch):
    """A broken OpenAI TwiML build must never drop the call - see the same
    guarantee ConversationRelay's build failure already has."""
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", True)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    session.add(
        GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111")
    )
    session.commit()

    monkeypatch.setattr(
        "app.communications.voice_webhooks.build_openai_voice_twiml",
        lambda garage: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    resp = _incoming(client, {"To": "+441111111111", "From": "+447700900000", "CallSid": "CA-ai-4"})

    assert resp.status_code == 200
    assert b"api/ws/twilio/openai-voice" not in resp.data
