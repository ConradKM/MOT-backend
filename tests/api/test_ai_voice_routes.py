"""POST /api/webhooks/openai/realtime - the OpenAI Realtime SIP webhook
(app/ai_voice/routes.py).

``verify_webhook`` itself (signature/timestamp checking) is the openai SDK's
own, already-tested code (see app/ai_voice/openai_sip.py) - these tests
monkeypatch it to focus on this route's own logic: event-type handling,
idempotency, tenant resolution, accept/reject, and call-controller spawning.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai import InvalidWebhookSignatureError

from app.ai_voice import routes as ai_voice_routes
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)


def _incoming_event(call_id="rtc_test_1", *, to="+442012345678", from_="+447123456789"):
    return SimpleNamespace(
        id="evt_1",
        type="realtime.call.incoming",
        data=SimpleNamespace(
            call_id=call_id,
            sip_headers=[
                {"name": "To", "value": f"sip:{to}@sip.example.com"},
                {"name": "From", "value": f"sip:{from_}@sip.example.com"},
            ],
        ),
    )


@pytest.fixture(autouse=True)
def _enabled(app, monkeypatch):
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", True)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", "whsec_test")


@pytest.fixture(autouse=True)
def _no_real_call_controller(monkeypatch):
    """Never actually connect to OpenAI's realtime WS from a test - run
    whatever gevent would have spawned synchronously instead, against a
    mocked run_call_controller."""
    monkeypatch.setattr(ai_voice_routes.gevent, "spawn", lambda fn: fn())
    mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "run_call_controller", mock)
    return mock


def _mapped_garage(session, garage, *, enabled=True, voice_number="+442012345678"):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id, communications_enabled=enabled, voice_phone_number=voice_number
        )
    )
    session.commit()


def test_webhook_not_configured_without_a_secret(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", "")
    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")
    assert resp.status_code == 503


def test_invalid_signature_is_rejected(client, monkeypatch):
    monkeypatch.setattr(
        ai_voice_routes,
        "verify_webhook",
        Mock(side_effect=InvalidWebhookSignatureError("bad signature")),
    )
    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")
    assert resp.status_code == 400


def test_unsupported_event_type_is_ignored(client, monkeypatch):
    monkeypatch.setattr(
        ai_voice_routes,
        "verify_webhook",
        Mock(return_value=SimpleNamespace(type="response.completed", id="evt_x")),
    )
    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")
    assert resp.status_code == 200
    assert CommunicationLog.query.count() == 0


def test_known_number_accepts_the_call_and_logs_it(
    session, garage, client, monkeypatch, _no_real_call_controller
):
    _mapped_garage(session, garage)
    monkeypatch.setattr(
        ai_voice_routes, "verify_webhook", Mock(return_value=_incoming_event(call_id="rtc_known_1"))
    )
    accept_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "accept_call", accept_mock)

    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")

    assert resp.status_code == 200
    accept_mock.assert_called_once()
    assert accept_mock.call_args.args[0] == "rtc_known_1"

    log = CommunicationLog.query.filter_by(external_id="rtc_known_1").one()
    assert log.garage_id == garage.id
    assert log.channel == "VOICE"
    assert log.direction == "INBOUND"
    assert log.external_provider == "openai"
    assert log.from_address == "+447123456789"

    _no_real_call_controller.assert_called_once()
    call_kwargs = _no_real_call_controller.call_args.kwargs
    assert call_kwargs["call_id"] == "rtc_known_1"
    assert call_kwargs["garage"].id == garage.id
    assert call_kwargs["caller_phone"] == "+447123456789"


def test_unknown_number_rejects_the_call(client, monkeypatch):
    monkeypatch.setattr(
        ai_voice_routes,
        "verify_webhook",
        Mock(return_value=_incoming_event(call_id="rtc_unknown_1", to="+441614969999")),
    )
    reject_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "reject_call", reject_mock)
    accept_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "accept_call", accept_mock)

    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")

    assert resp.status_code == 200
    reject_mock.assert_called_once_with("rtc_unknown_1", status_code=None)
    accept_mock.assert_not_called()
    assert CommunicationLog.query.filter_by(external_id="rtc_unknown_1").first() is None


def test_disabled_business_rejects_the_call(session, garage, client, monkeypatch):
    _mapped_garage(session, garage, enabled=False)
    monkeypatch.setattr(
        ai_voice_routes,
        "verify_webhook",
        Mock(return_value=_incoming_event(call_id="rtc_disabled_1")),
    )
    reject_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "reject_call", reject_mock)
    accept_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "accept_call", accept_mock)

    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")

    assert resp.status_code == 200
    reject_mock.assert_called_once()
    accept_mock.assert_not_called()


def test_duplicate_call_id_delivery_is_a_no_op(
    session, garage, client, monkeypatch, _no_real_call_controller
):
    _mapped_garage(session, garage)
    monkeypatch.setattr(
        ai_voice_routes, "verify_webhook", Mock(return_value=_incoming_event(call_id="rtc_dup_1"))
    )
    accept_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "accept_call", accept_mock)

    first = client.post("/api/webhooks/openai/realtime", data=b"{}")
    second = client.post("/api/webhooks/openai/realtime", data=b"{}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert accept_mock.call_count == 1  # never accepted twice
    assert CommunicationLog.query.filter_by(external_id="rtc_dup_1").count() == 1
    _no_real_call_controller.assert_called_once()


def test_accept_failure_rejects_instead_of_crashing(session, garage, client, monkeypatch):
    _mapped_garage(session, garage)
    monkeypatch.setattr(
        ai_voice_routes,
        "verify_webhook",
        Mock(return_value=_incoming_event(call_id="rtc_accept_fail_1")),
    )
    monkeypatch.setattr(
        ai_voice_routes,
        "accept_call",
        Mock(side_effect=ai_voice_routes.OpenAIVoiceError("boom")),
    )
    reject_mock = Mock()
    monkeypatch.setattr(ai_voice_routes, "reject_call", reject_mock)

    resp = client.post("/api/webhooks/openai/realtime", data=b"{}")

    assert resp.status_code == 200
    reject_mock.assert_called_once()
    assert CommunicationLog.query.filter_by(external_id="rtc_accept_fail_1").first() is None
