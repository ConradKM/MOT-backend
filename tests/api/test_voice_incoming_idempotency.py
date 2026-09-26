"""A duplicate delivery of Twilio's initial inbound-call webhook (the caller
never sees a retry, but Twilio's own retry policy re-POSTs /incoming with the
identical CallSid if it didn't get a fast, valid response the first time -
a slow response, a transient network blip on the reply, or CoMaz's own
process restarting mid-request) must be handled exactly like every other
webhook in this system: idempotently, never as a 500.

app/models/communications/communication_log.py::CommunicationLog.external_id
is unique - the same design update_communication_status already relies on
for idempotent status callbacks - but the /incoming route's own
record_inbound_communication() call was a bare INSERT with no such handling,
so a genuine duplicate CallSid delivery crashed with an unhandled
IntegrityError instead of a clean 200/replay.
"""

from app.communications.service import record_inbound_communication
from app.models.communications.communication_log import (
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)

VOICE_NUMBER = "+441611234567"
CALLER = "+447700900123"


def _configure_twilio(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", "https://api.example.test")


def test_a_redelivered_incoming_call_webhook_is_not_a_500(
    app, client, session, garage, monkeypatch
):
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number=VOICE_NUMBER))
    session.commit()
    _configure_twilio(app, monkeypatch)

    payload = {"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CAduplicate0001"}

    first = client.post("/api/webhooks/twilio/voice/incoming", data=payload)
    assert first.status_code == 200

    # Twilio redelivers the exact same webhook (same CallSid) - this must
    # behave the same way, not crash.
    second = client.post("/api/webhooks/twilio/voice/incoming", data=payload)
    assert second.status_code == 200
    assert second.get_data(as_text=True) == first.get_data(as_text=True)

    rows = CommunicationLog.query.filter_by(external_id="CAduplicate0001").all()
    assert len(rows) == 1, "a redelivered webhook must never create a second log row"


def test_three_redeliveries_still_leave_exactly_one_log_row(
    app, client, session, garage, monkeypatch
):
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number=VOICE_NUMBER))
    session.commit()
    _configure_twilio(app, monkeypatch)

    payload = {"To": VOICE_NUMBER, "From": CALLER, "CallSid": "CAduplicate0002"}

    for _ in range(3):
        resp = client.post("/api/webhooks/twilio/voice/incoming", data=payload)
        assert resp.status_code == 200

    rows = CommunicationLog.query.filter_by(external_id="CAduplicate0002").all()
    assert len(rows) == 1


def test_the_shared_helper_is_idempotent_for_sms_and_whatsapp_too(app, session, garage):
    """record_inbound_communication is the one function all three inbound
    webhook routes (voice, SMS, WhatsApp) call - proving it directly covers
    every caller without needing a full HTTP fixture per channel."""
    for channel, external_id in (
        (CHANNEL_SMS, "SMduplicate0001"),
        (CHANNEL_WHATSAPP, "SMduplicate0002"),
    ):
        first = record_inbound_communication(
            garage=garage,
            channel=channel,
            from_address="+447700900123",
            to_address="+447700900456",
            external_id=external_id,
            status="received",
            body="hello",
        )
        second = record_inbound_communication(
            garage=garage,
            channel=channel,
            from_address="+447700900123",
            to_address="+447700900456",
            external_id=external_id,
            status="received",
            body="hello",
        )
        assert first.id == second.id
        rows = CommunicationLog.query.filter_by(external_id=external_id).all()
        assert len(rows) == 1
