"""Unit tests for the app/communications service layer - config gating,
tenant resolution, the event dispatcher, and the Twilio-facing send/log
functions. None of this has an HTTP surface of its own yet, so it's tested
by importing and calling it directly (the same style as tests/database/*),
with the Twilio client itself replaced by a fake (monkeypatch.setattr, the
same pattern tests/api/test_public_booking.py uses for the CAPTCHA provider).

tests/api/test_twilio_webhooks.py covers the HTTP/webhook layer built on top
of this module.
"""

from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException

from app.communications import events as comms_events
from app.communications.config import garage_communications_enabled, is_twilio_configured
from app.communications.service import (
    find_customer_by_phone,
    initiate_voice_call,
    send_whatsapp_message,
    update_communication_status,
)
from app.communications.tenant_resolution import (
    resolve_garage_by_voice_number,
    resolve_garage_by_whatsapp_sender,
)
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_OUTBOUND,
    STATUS_SKIPPED_NOT_CONFIGURED,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)


@pytest.fixture()
def comms_settings(session, garage):
    """The primary garage, fully configured for outbound sends - individual
    tests still have to make Twilio itself "configured" (TWILIO_ACCOUNT_SID/
    TWILIO_AUTH_TOKEN), since that's a deployment-level setting, not a
    per-garage one."""
    settings = GarageCommunicationSettings(
        garage_id=garage.id,
        communications_enabled=True,
        whatsapp_sender="whatsapp:+14155238886",
        voice_phone_number="+441234567890",
    )
    session.add(settings)
    session.commit()
    session.refresh(garage)
    return settings


def _configure_twilio(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "test-auth-token")


class _FakeMessages:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeCalls:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


# --------------------------------------------------------------------------
# is_twilio_configured / garage_communications_enabled
# --------------------------------------------------------------------------


def test_is_twilio_configured_false_by_default(app):
    assert is_twilio_configured() is False


def test_is_twilio_configured_true_once_both_set(app, monkeypatch):
    _configure_twilio(app, monkeypatch)
    assert is_twilio_configured() is True


def test_is_twilio_configured_false_with_only_sid(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    assert is_twilio_configured() is False


def test_garage_communications_enabled_false_with_no_settings_row(garage):
    assert garage_communications_enabled(garage) is False


def test_garage_communications_enabled_true_once_opted_in(comms_settings, garage):
    assert garage_communications_enabled(garage) is True


# --------------------------------------------------------------------------
# send_whatsapp_message
# --------------------------------------------------------------------------


def test_send_whatsapp_skips_when_twilio_not_configured(garage):
    log = send_whatsapp_message(garage=garage, to="07123456789", body="hi")
    assert log.status == STATUS_SKIPPED_NOT_CONFIGURED
    assert log.external_id is None
    assert "not configured" in log.error_message.lower()


def test_send_whatsapp_skips_when_garage_not_enabled(app, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    log = send_whatsapp_message(garage=garage, to="07123456789", body="hi")
    assert log.status == STATUS_SKIPPED_NOT_CONFIGURED
    assert "not enabled" in log.error_message.lower()


def test_send_whatsapp_skips_when_no_sender_configured(app, session, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    session.add(GarageCommunicationSettings(garage_id=garage.id, communications_enabled=True))
    session.commit()
    session.refresh(garage)

    log = send_whatsapp_message(garage=garage, to="07123456789", body="hi")
    assert log.status == STATUS_SKIPPED_NOT_CONFIGURED
    assert "sender" in log.error_message.lower()


def test_send_whatsapp_skips_on_invalid_number(app, comms_settings, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    log = send_whatsapp_message(garage=garage, to="not a number", body="hi")
    assert log.status == STATUS_SKIPPED_NOT_CONFIGURED
    assert "invalid destination number" in log.error_message.lower()


def test_send_whatsapp_success_records_log(app, comms_settings, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    fake_client = SimpleNamespace(
        messages=_FakeMessages(result=SimpleNamespace(sid="SM123", status="queued"))
    )
    monkeypatch.setattr(
        "app.communications.service.get_twilio_client_for_garage", lambda garage: fake_client
    )

    log = send_whatsapp_message(garage=garage, to="07123456789", body="Hi there")

    assert log.status == "queued"
    assert log.external_id == "SM123"
    assert log.channel == CHANNEL_WHATSAPP
    assert log.direction == DIRECTION_OUTBOUND
    assert log.to_address == "whatsapp:+447123456789"
    assert log.from_address == "whatsapp:+14155238886"
    assert log.body == "Hi there"
    assert fake_client.messages.calls == [
        {"to": "whatsapp:+447123456789", "body": "Hi there", "from_": "whatsapp:+14155238886"}
    ]


def test_send_whatsapp_prefers_messaging_service_sid_when_set(
    app, session, garage, comms_settings, monkeypatch
):
    _configure_twilio(app, monkeypatch)
    comms_settings.messaging_service_sid = "MG" + "0" * 32
    session.commit()

    fake_client = SimpleNamespace(
        messages=_FakeMessages(result=SimpleNamespace(sid="SM124", status="queued"))
    )
    monkeypatch.setattr(
        "app.communications.service.get_twilio_client_for_garage", lambda garage: fake_client
    )

    send_whatsapp_message(garage=garage, to="07123456789", body="Hi")

    call = fake_client.messages.calls[0]
    assert call["messaging_service_sid"] == comms_settings.messaging_service_sid
    assert "from_" not in call


def test_send_whatsapp_records_twilio_failure(app, comms_settings, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    exc = TwilioRestException(status=400, uri="/Messages", msg="Invalid number", code=21211)
    fake_client = SimpleNamespace(messages=_FakeMessages(exc=exc))
    monkeypatch.setattr(
        "app.communications.service.get_twilio_client_for_garage", lambda garage: fake_client
    )

    log = send_whatsapp_message(garage=garage, to="07123456789", body="Hi")

    assert log.status == "FAILED"
    assert log.error_code == "21211"
    assert log.error_message == "Invalid number"
    assert log.external_id is None


# --------------------------------------------------------------------------
# initiate_voice_call
# --------------------------------------------------------------------------


def test_initiate_voice_call_skips_when_not_configured(garage):
    log = initiate_voice_call(garage=garage, to="07123456789", twiml_url="https://example.test/twiml")
    assert log.status == STATUS_SKIPPED_NOT_CONFIGURED
    assert log.channel == CHANNEL_VOICE


def test_initiate_voice_call_success_records_log(app, comms_settings, garage, monkeypatch):
    _configure_twilio(app, monkeypatch)
    fake_client = SimpleNamespace(
        calls=_FakeCalls(result=SimpleNamespace(sid="CA123", status="queued"))
    )
    monkeypatch.setattr(
        "app.communications.service.get_twilio_client_for_garage", lambda garage: fake_client
    )

    log = initiate_voice_call(garage=garage, to="07123456789", twiml_url="https://example.test/twiml")

    assert log.status == "queued"
    assert log.external_id == "CA123"
    assert log.from_address == "+441234567890"
    assert log.to_address == "+447123456789"


# --------------------------------------------------------------------------
# update_communication_status
# --------------------------------------------------------------------------


def test_update_communication_status_updates_matching_row(session, garage):
    log = CommunicationLog(
        garage_id=garage.id, channel=CHANNEL_VOICE, direction=DIRECTION_OUTBOUND,
        status="queued", external_id="CA999",
    )
    session.add(log)
    session.commit()

    updated = update_communication_status(
        external_id="CA999", status="completed", call_duration_seconds=42
    )

    assert updated.id == log.id
    assert updated.status == "completed"
    assert updated.call_duration_seconds == 42


def test_update_communication_status_is_idempotent_across_duplicate_callbacks(session, garage):
    session.add(
        CommunicationLog(
            garage_id=garage.id, channel=CHANNEL_VOICE, direction=DIRECTION_OUTBOUND,
            status="queued", external_id="CA998",
        )
    )
    session.commit()

    update_communication_status(external_id="CA998", status="ringing")
    update_communication_status(external_id="CA998", status="completed", call_duration_seconds=10)

    matches = CommunicationLog.query.filter_by(external_id="CA998").all()
    assert len(matches) == 1
    assert matches[0].status == "completed"


def test_update_communication_status_unknown_external_id_is_safely_ignored(app):
    assert update_communication_status(external_id="does-not-exist", status="failed") is None


# --------------------------------------------------------------------------
# find_customer_by_phone
# --------------------------------------------------------------------------


def test_find_customer_by_phone_matches_exact_string(garage, customer):
    found = find_customer_by_phone(garage, customer.phone)
    assert found is not None
    assert found.id == customer.id


def test_find_customer_by_phone_no_match_returns_none(garage):
    assert find_customer_by_phone(garage, "+447700900999") is None


def test_find_customer_by_phone_is_tenant_scoped(second_garage, customer):
    assert find_customer_by_phone(second_garage, customer.phone) is None


# --------------------------------------------------------------------------
# tenant_resolution
# --------------------------------------------------------------------------


def test_resolve_garage_by_voice_number(session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, voice_phone_number="+441111111111"))
    session.commit()

    resolved = resolve_garage_by_voice_number("+441111111111")
    assert resolved is not None
    assert resolved.id == garage.id


def test_resolve_garage_by_voice_number_unknown_number_returns_none(garage):
    assert resolve_garage_by_voice_number("+449999999999") is None


def test_resolve_garage_by_whatsapp_sender_is_tenant_isolated(session, garage, second_garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id, whatsapp_sender="whatsapp:+10000000001"))
    session.add(
        GarageCommunicationSettings(garage_id=second_garage.id, whatsapp_sender="whatsapp:+10000000002")
    )
    session.commit()

    resolved = resolve_garage_by_whatsapp_sender("whatsapp:+10000000002")
    assert resolved.id == second_garage.id


# --------------------------------------------------------------------------
# events - booking-lifecycle hook points
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_event_handlers():
    comms_events._reset_handlers_for_tests()
    yield
    comms_events._reset_handlers_for_tests()


def test_emit_event_with_no_handlers_is_a_no_op(garage):
    comms_events.emit_event(comms_events.BOOKING_REQUEST_CREATED, garage=garage)


def test_register_handler_receives_the_emitted_event(garage):
    received = []
    comms_events.register_handler(
        comms_events.BOOKING_REQUEST_APPROVED,
        lambda garage, **context: received.append((garage, context)),
    )

    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_APPROVED, garage=garage, booking_request="BR1"
    )

    assert received == [(garage, {"booking_request": "BR1"})]


def test_emit_event_swallows_handler_exceptions(garage):
    def boom(garage, **context):
        raise RuntimeError("boom")

    comms_events.register_handler(comms_events.APPOINTMENT_CANCELLED, boom)

    comms_events.emit_event(comms_events.APPOINTMENT_CANCELLED, garage=garage)


def test_register_handler_rejects_unknown_event_type():
    with pytest.raises(ValueError):
        comms_events.register_handler("NOT_A_REAL_EVENT", lambda **context: None)
