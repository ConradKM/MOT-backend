"""app/ws/openai_voice.py's tenant-resolution safety, and the TwiML builder
in app/ai_voice/twiml.py. The full WebSocket bridge loop (audio relay,
OpenAI session lifecycle) needs a live Realtime connection to exercise
meaningfully and is out of scope for these unit tests - see
docs/OPENAI_VOICE.md's manual test-call section for that.
"""

from app.ai_voice.twiml import build_openai_voice_twiml
from app.models.communications.communication_log import CommunicationLog
from app.ws.openai_voice import _resolve_call_garage


def test_resolve_call_garage_requires_both_ids(garage):
    assert _resolve_call_garage(None, "CA123") is None
    assert _resolve_call_garage(str(garage.id), None) is None
    assert _resolve_call_garage(None, None) is None


def test_resolve_call_garage_rejects_an_unknown_garage_id(app):
    assert _resolve_call_garage("00000000-0000-0000-0000-000000000000", "CA123") is None


def test_resolve_call_garage_rejects_a_malformed_garage_id(app):
    assert _resolve_call_garage("not-a-uuid", "CA123") is None


def test_resolve_call_garage_requires_a_matching_logged_call(garage):
    # The garage exists, but no /incoming webhook ever logged this CallSid -
    # a bare WebSocket connection can't manufacture that row itself.
    assert _resolve_call_garage(str(garage.id), "CA-never-logged") is None


def test_resolve_call_garage_succeeds_once_the_call_is_logged(session, garage):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel="VOICE",
            direction="INBOUND",
            external_provider="twilio",
            external_id="CA-real-1",
            from_address="+447123456789",
            status="in-progress",
        )
    )
    session.commit()

    resolved = _resolve_call_garage(str(garage.id), "CA-real-1")
    assert resolved is not None
    assert resolved.id == garage.id


def test_resolve_call_garage_rejects_a_call_logged_for_a_different_garage(
    session, garage, second_garage
):
    session.add(
        CommunicationLog(
            garage_id=second_garage.id,
            channel="VOICE",
            direction="INBOUND",
            external_provider="twilio",
            external_id="CA-tenant-b",
            status="in-progress",
        )
    )
    session.commit()

    # Claiming garage A's id for a call actually logged under garage B fails -
    # exactly the spoofing attempt this function exists to block.
    assert _resolve_call_garage(str(garage.id), "CA-tenant-b") is None


def test_build_openai_voice_twiml_connects_a_stream_with_the_garage_id(app, garage):
    app.config["PUBLIC_API_BASE_URL"] = "https://api.example.test"
    twiml = build_openai_voice_twiml(garage)
    assert "<Connect>" in twiml
    assert "wss://api.example.test/api/ws/twilio/openai-voice" in twiml
    assert str(garage.id) in twiml
