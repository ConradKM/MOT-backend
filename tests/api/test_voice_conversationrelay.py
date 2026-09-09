"""ConversationRelay voice: the inbound-call TwiML switch and the
WebSocket <-> conversation-engine bridge (app/communications/voice_relay.py,
app/ws/twilio_voice.py).

The engine itself is exercised in tests/test_conversation_engine.py against
the exact same engine.handle_message() call - here we only prove the Voice
*transport*: correct TwiML, the static fallback, tenant isolation, turn
framing, and safe teardown. TestConfig sets TWILIO_WEBHOOK_VALIDATE = False,
so the handshake signature check is exercised in its own test with it flipped
back on.
"""

import json

import pytest

from app.communications import voice_relay
from app.communications.security import validate_twilio_websocket
from app.communications.voice_relay import bridge_ws_url
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.conversation.callback_request import CallbackRequest
from app.models.conversation.conversation_session import ConversationSession
from app.ws.twilio_voice import twilio_voice_bridge

VOICE_NUMBER = "+441611234567"
CALLER = "+447700900123"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class FakeWs:
    """Stands in for a flask-sock connection: `receive()` yields the scripted
    frames (dicts are JSON-encoded) then None (closed); `send()` records."""

    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent: list[dict] = []

    def receive(self, timeout=None):
        if not self._in:
            return None
        item = self._in.pop(0)
        return item if isinstance(item, str) else json.dumps(item)

    def send(self, data):
        self.sent.append(json.loads(data))

    def tokens(self):
        return [m["token"] for m in self.sent if m.get("type") == "text"]


def _setup(**over):
    msg = {
        "type": "setup",
        "callSid": "CAtest0001",
        "from": CALLER,
        "to": VOICE_NUMBER,
        "direction": "inbound",
        "customParameters": {},
    }
    msg.update(over)
    return msg


def _prompt(text, last=True):
    return {"type": "prompt", "voicePrompt": text, "lang": "en-GB", "last": last}


@pytest.fixture()
def voice_business(session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number=VOICE_NUMBER))
    session.commit()
    session.refresh(garage)
    return garage


def _run_bridge(app, incoming):
    ws = FakeWs(incoming)
    with app.test_request_context("/api/ws/twilio/voice"):
        twilio_voice_bridge(ws)
    return ws


# --------------------------------------------------------------------------
# inbound-call TwiML switch (app/communications/voice_webhooks.py)
# --------------------------------------------------------------------------


def _configure_twilio(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", "https://api.example.test")


def test_incoming_call_static_greeting_when_conversationrelay_disabled(
    app, client, voice_business, monkeypatch
):
    _configure_twilio(app, monkeypatch)  # CONVERSATIONRELAY_ENABLED stays False

    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CA1"},
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Say>" in body and "ConversationRelay" not in body


def test_incoming_call_returns_conversationrelay_twiml_when_enabled(
    app, client, voice_business, appointment_type, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "TWILIO_CONVERSATIONRELAY_ENABLED", True)

    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CA1"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "<Connect>" in body and "<ConversationRelay" in body
    assert 'url="wss://api.example.test/api/ws/twilio/voice"' in body
    assert 'language="en-GB"' in body
    assert voice_business.name in body  # dynamic greeting
    assert f'value="{voice_business.id}"' in body  # garage_id parameter
    assert appointment_type.name in body  # a speech hint


def test_incoming_call_falls_back_to_static_greeting_when_twiml_build_raises(
    app, client, voice_business, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "TWILIO_CONVERSATIONRELAY_ENABLED", True)
    monkeypatch.setattr(
        "app.communications.voice_webhooks.build_incoming_call_twiml",
        lambda garage: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    resp = client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CA1"},
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Say>" in body and "ConversationRelay" not in body


def test_bridge_ws_url_swaps_scheme(app, monkeypatch):
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", "https://mot-backend.onrender.com")
    assert bridge_ws_url() == "wss://mot-backend.onrender.com/api/ws/twilio/voice"


# --------------------------------------------------------------------------
# handshake signature
# --------------------------------------------------------------------------


def test_ws_signature_validation(app, monkeypatch):
    from twilio.request_validator import RequestValidator

    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "TWILIO_WEBHOOK_VALIDATE", True)
    url = bridge_ws_url()

    with app.test_request_context("/api/ws/twilio/voice"):
        from flask import request

        assert validate_twilio_websocket(request, url) is False  # no header

    good = RequestValidator("test-token").compute_signature(url, {})
    with app.test_request_context("/api/ws/twilio/voice", headers={"X-Twilio-Signature": good}):
        from flask import request

        assert validate_twilio_websocket(request, url) is True

    with app.test_request_context("/api/ws/twilio/voice", headers={"X-Twilio-Signature": "wrong"}):
        from flask import request

        assert validate_twilio_websocket(request, url) is False


def test_bridge_rejects_a_connection_with_a_bad_signature(app, voice_business, monkeypatch):
    _configure_twilio(app, monkeypatch)
    monkeypatch.setitem(app.config, "TWILIO_WEBHOOK_VALIDATE", True)

    ws = FakeWs([_setup(), _prompt("I need an MOT")])
    with app.test_request_context("/api/ws/twilio/voice", headers={"X-Twilio-Signature": "nope"}):
        twilio_voice_bridge(ws)

    # Rejected before reading `setup` - nothing sent, no session created.
    assert ws.sent == []
    assert ConversationSession.query.count() == 0


# --------------------------------------------------------------------------
# the bridge: transport behaviour
# --------------------------------------------------------------------------


def test_caller_text_reaches_the_engine_and_a_reply_is_spoken(app, voice_business):
    ws = _run_bridge(app, [_setup(), _prompt("what are your opening hours")])

    assert ws.tokens(), "expected a spoken reply"
    session = ConversationSession.query.filter_by(
        garage_id=voice_business.id, channel="VOICE", customer_phone=CALLER
    ).one()
    assert session.intent == "BUSINESS_HOURS_QUERY"


def test_session_persists_across_turns(app, voice_business, appointment_type, garage_schedule):
    ws = _run_bridge(
        app,
        [
            _setup(),
            _prompt("i want to book an mot"),
            _prompt("what are your opening hours"),
        ],
    )

    # One session row, used by both turns.
    sessions = ConversationSession.query.filter_by(garage_id=voice_business.id).all()
    assert len(sessions) == 1
    # Two engine turns => at least two spoken replies.
    assert len(ws.tokens()) >= 2


def test_partial_prompt_is_ignored_until_last(app, voice_business):
    ws = _run_bridge(
        app,
        [_setup(), _prompt("what are", last=False), _prompt("what are your hours", last=True)],
    )
    # Only the completed utterance produced a turn.
    assert len(ws.tokens()) == 1


def test_unknown_number_is_told_it_is_not_automated(app, garage):
    # `garage` has no GarageCommunicationSettings row -> unresolved.
    ws = _run_bridge(app, [_setup(to="+449999999999"), _prompt("hello")])
    assert any("not set up for automated booking" in t for t in ws.tokens())
    assert ConversationSession.query.count() == 0


def test_spoofed_garage_id_parameter_is_rejected(app, voice_business, second_garage):
    ws = _run_bridge(
        app,
        [_setup(customParameters={"garage_id": str(second_garage.id)}), _prompt("hi")],
    )
    assert any("not set up for automated booking" in t for t in ws.tokens())
    assert ConversationSession.query.count() == 0


def test_caller_hangup_after_setup_exits_cleanly(app, voice_business):
    ws = FakeWs([_setup()])  # setup, then receive() -> None
    with app.test_request_context("/api/ws/twilio/voice"):
        twilio_voice_bridge(ws)  # must not raise
    assert ws.sent == []


def test_engine_failure_speaks_an_apology_and_creates_a_callback(app, voice_business, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("app.ws.twilio_voice.engine.handle_message", _boom)

    ws = _run_bridge(app, [_setup(), _prompt("book me an mot")])

    assert any("having trouble" in t.lower() for t in ws.tokens())
    assert any(m.get("type") == "end" for m in ws.sent)
    cb = CallbackRequest.query.filter_by(garage_id=voice_business.id).one()
    assert cb.phone_number == CALLER


def test_speak_to_human_hands_off_and_ends_the_call(app, voice_business):
    ws = _run_bridge(app, [_setup(), _prompt("I want to speak to a real person")])
    assert ws.tokens()  # the engine speaks a "team will help" line first

    # A later turn on the same phone (a distinct call) - the session is now in
    # HUMAN_HANDOFF, so the bridge speaks the close-out and ends the call.
    ws2 = _run_bridge(app, [_setup(callSid="CAtest0002"), _prompt("hello are you there")])

    assert any(m.get("type") == "end" for m in ws2.sent)
    assert CallbackRequest.query.filter_by(garage_id=voice_business.id).count() == 1


def test_call_log_row_is_tagged_with_the_detected_intent(app, client, voice_business, monkeypatch):
    _configure_twilio(app, monkeypatch)
    # The /incoming webhook creates the call's CommunicationLog row.
    client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CAtest0001"},
    )

    _run_bridge(app, [_setup(), _prompt("what are your opening hours")])

    row = CommunicationLog.query.filter_by(
        garage_id=voice_business.id, external_id="CAtest0001"
    ).first()
    assert row is not None
    assert row.intent == "BUSINESS_HOURS_QUERY"


def test_transcript_turns_carry_the_call_sid_for_grouping(app, client, voice_business, monkeypatch):
    _configure_twilio(app, monkeypatch)
    client.post(
        "/api/webhooks/twilio/voice/incoming",
        data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CAtest0001"},
    )

    _run_bridge(
        app,
        [
            _setup(),
            _prompt("what are your opening hours"),
            _prompt("and where are you based"),
        ],
    )

    rows = CommunicationLog.query.filter_by(garage_id=voice_business.id, channel="VOICE").all()
    # The call-level row plus every engine transcript turn share one call_sid.
    assert rows
    assert all(r.call_sid == "CAtest0001" for r in rows)
    # And exactly one of them is the call-level row (not an engine turn).
    call_level = [r for r in rows if r.external_provider != "comaz_conversation_engine"]
    assert len(call_level) == 1


def test_dtmf_digit_is_fed_to_the_engine_as_text(app, voice_business):
    ws = _run_bridge(app, [_setup(), {"type": "dtmf", "dtmf": "1"}])
    # A lone "1" is an unresolved turn - the engine still answers rather than
    # ignoring it, proving the DTMF frame reached handle_message.
    assert ws.tokens()


def test_reference_to_voice_relay_module_is_importable():
    assert hasattr(voice_relay, "build_incoming_call_twiml")
