"""The configurable phone menu (IVR) - app/communications/ivr, the /ivr/*
Twilio webhooks in app/communications/voice_webhooks.py, and the signed
handoff into the OpenAI SIP agent (app/ai_voice/sip_handoff.py).

TestConfig sets TWILIO_WEBHOOK_VALIDATE = False, so these tests drive the
webhooks directly; tenant resolution still comes from the ``To`` number.
"""

import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.ai_voice import call_controller, sip_handoff
from app.ai_voice import routes as ai_voice_routes
from app.communications import queries
from app.communications.ivr import service as ivr_service
from app.extensions import db
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.communications.voice_ivr_settings import GarageVoiceIvrSettings
from app.models.conversation.callback_request import CallbackRequest
from app.models.employee import Employee

VOICE_A = "+441611234567"
VOICE_B = "+441619876543"
CALLER = "+447700900123"
RECEPTION_A = "+441612223333"
RECEPTION_B = "+441614445555"
PROJECT = "proj_test123"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def voice_config(app, monkeypatch):
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setitem(app.config, "PUBLIC_API_BASE_URL", "https://api.example.test")
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", True)
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setitem(app.config, "OPENAI_PROJECT_ID", PROJECT)


def _comms(session, garage, number, *, escalation=None):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            communications_enabled=True,
            voice_phone_number=number,
            voice_escalation_number=escalation,
        )
    )
    session.commit()
    session.refresh(garage)


def _menu(session, garage, reception, **over):
    settings = GarageVoiceIvrSettings(
        garage_id=garage.id,
        enabled=True,
        greeting=f"Welcome to {garage.name}.",
        options=[
            {
                "digit": "1",
                "label": "bookings",
                "prompt": None,
                "action": "AI_BOOKING",
                "target": None,
            },
            {
                "digit": "2",
                "label": "reception",
                "prompt": "To speak to us, press 2.",
                "action": "HUMAN_TRANSFER",
                "target": reception,
            },
            {
                "digit": "9",
                "label": "the menu again",
                "prompt": None,
                "action": "REPEAT_MENU",
                "target": None,
            },
        ],
        fallback_action="HUMAN_TRANSFER",
        fallback_target=None,
        max_attempts=2,
    )
    for key, value in over.items():
        setattr(settings, key, value)
    session.add(settings)
    session.commit()
    return settings


@pytest.fixture()
def business_a(session, garage, voice_config):
    _comms(session, garage, VOICE_A)
    _menu(session, garage, RECEPTION_A)
    return garage


@pytest.fixture()
def business_b(session, second_garage, voice_config):
    _comms(session, second_garage, VOICE_B)
    _menu(session, second_garage, RECEPTION_B, greeting="Hello from B.")
    return second_garage


def _post(client, step, *, to=VOICE_A, call_sid="CAivr0001", query="", **form):
    data = {"To": to, "From": CALLER, "CallSid": call_sid, **form}
    return client.post(f"/api/webhooks/twilio/voice/{step}{query}", data=data)


def _body(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _sip_headers_from_twiml(body):
    """The X-CoMaz-* headers exactly as Twilio would attach them to the
    INVITE, parsed out of the <Sip> URI in the TwiML."""
    start = body.index("<Sip>") + len("<Sip>")
    uri = body[start : body.index("</Sip>")].replace("&amp;", "&")
    query = urlsplit(uri.replace(";transport=tls?", "?")).query
    return [{"name": k, "value": unquote(v[0])} for k, v in parse_qs(query).items()]


# --------------------------------------------------------------------------
# Inbound: the menu runs before any AI
# --------------------------------------------------------------------------


def test_no_menu_row_keeps_the_pre_menu_behaviour(session, garage, client, voice_config):
    _comms(session, garage, VOICE_A)
    body = _body(_post(client, "incoming"))
    assert "<Gather" not in body
    assert "currently being configured" in body


def test_disabled_menu_keeps_the_pre_menu_behaviour(session, garage, client, voice_config):
    _comms(session, garage, VOICE_A)
    _menu(session, garage, RECEPTION_A, enabled=False)
    body = _body(_post(client, "incoming"))
    assert "<Gather" not in body


def test_enabled_menu_plays_greeting_and_options_before_any_ai(client, business_a):
    body = _body(_post(client, "incoming"))
    assert "<Gather" in body and 'numDigits="1"' in body
    assert "Welcome to Garage A." in body
    assert "For bookings, press 1." in body
    assert "To speak to us, press 2." in body
    assert "/ivr/menu?attempt=1" in body
    # Nothing AI-related happens until a digit is pressed.
    assert "<Sip>" not in body and "ConversationRelay" not in body


def test_each_tenant_hears_its_own_menu(client, business_a, business_b):
    assert "Welcome to Garage A." in _body(_post(client, "incoming", to=VOICE_A))
    body_b = _body(_post(client, "incoming", to=VOICE_B, call_sid="CAivrB"))
    assert "Hello from B." in body_b and "Garage A" not in body_b


# --------------------------------------------------------------------------
# Digit handling
# --------------------------------------------------------------------------


def test_digit_1_bridges_to_the_ai_with_a_signed_tenant_handoff(client, business_a):
    body = _body(_post(client, "ivr/menu", query="?attempt=1", Digits="1"))
    assert "<Dial" in body and "<Sip>" in body
    assert f"sip:{PROJECT}@sip.api.openai.com;transport=tls?" in body
    assert "/ivr/ai-complete" in body

    handoff = sip_handoff.verify(_sip_headers_from_twiml(body))
    assert handoff is not None
    assert handoff.garage_id == str(business_a.id)
    assert handoff.twilio_call_sid == "CAivr0001"
    assert handoff.route == "AI_BOOKING"
    assert handoff.caller == CALLER


def test_digit_2_transfers_to_this_tenants_own_number(client, business_a, business_b):
    body_a = _body(_post(client, "ivr/menu", query="?attempt=1", Digits="2"))
    assert f"<Number>{RECEPTION_A}</Number>" in body_a
    assert "/ivr/transfer-complete" in body_a
    assert "<Sip>" not in body_a

    body_b = _body(_post(client, "ivr/menu", to=VOICE_B, query="?attempt=1", Digits="2"))
    assert f"<Number>{RECEPTION_B}</Number>" in body_b
    assert RECEPTION_A not in body_b


def test_query_string_cannot_pick_another_tenant(client, business_a, business_b):
    """Tenant always comes from the dialled number, never the URL."""
    body = _body(
        _post(
            client, "ivr/menu", to=VOICE_B, query=f"?attempt=1&garage={business_a.id}", Digits="2"
        )
    )
    assert RECEPTION_B in body and RECEPTION_A not in body


def test_invalid_digit_reprompts_then_falls_back_to_a_person(client, business_a):
    first = _body(_post(client, "ivr/menu", query="?attempt=1", Digits="7"))
    assert "that isn't one of the options" in first
    assert "attempt=2" in first

    last = _body(_post(client, "ivr/menu", query="?attempt=2", Digits="7"))
    assert "<Gather" not in last
    assert f"<Number>{RECEPTION_A}</Number>" in last


def test_no_input_reprompts_then_falls_back_to_a_person(client, business_a):
    first = _body(_post(client, "ivr/menu", query="?attempt=1", Digits=""))
    assert "didn't catch that" in first and "<Gather" in first

    last = _body(_post(client, "ivr/menu", query="?attempt=2"))
    assert f"<Number>{RECEPTION_A}</Number>" in last


def test_repeat_menu_replays_but_is_bounded(client, business_a):
    again = _body(_post(client, "ivr/menu", query="?attempt=1&repeat=0", Digits="9"))
    assert "<Gather" in again and "repeat=1" in again

    exhausted = _body(_post(client, "ivr/menu", query="?attempt=1&repeat=3", Digits="9"))
    assert "<Gather" not in exhausted
    assert f"<Number>{RECEPTION_A}</Number>" in exhausted


def test_ai_option_falls_back_to_a_person_when_ai_is_unavailable(
    app, client, business_a, monkeypatch
):
    monkeypatch.setitem(app.config, "OPENAI_VOICE_ENABLED", False)
    body = _body(_post(client, "ivr/menu", query="?attempt=1", Digits="1"))
    assert "<Sip>" not in body
    assert f"<Number>{RECEPTION_A}</Number>" in body


def test_no_destination_at_all_logs_a_callback_and_closes_politely(
    session, garage, client, voice_config
):
    _comms(session, garage, VOICE_A)
    _menu(
        session,
        garage,
        None,
        options=[
            {
                "digit": "1",
                "label": "bookings",
                "prompt": None,
                "action": "AI_BOOKING",
                "target": None,
            }
        ],
        fallback_action="AI_BOOKING",
    )
    # AI tried and failed; there is no person to transfer to.
    body = _body(
        _post(
            client, "ivr/ai-complete", query="?route=AI_BOOKING&tried=AI", DialCallStatus="failed"
        )
    )
    assert "<Hangup" in body and "call you back" in body
    assert [c.phone_number for c in CallbackRequest.query.filter_by(garage_id=garage.id)] == [
        CALLER
    ]


# --------------------------------------------------------------------------
# After the AI leg / after a transfer
# --------------------------------------------------------------------------


def _ai_leg(session, garage, *, status, twilio_sid="CAivr0001"):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel="VOICE",
            direction="INBOUND",
            external_provider="openai",
            external_id=f"rtc_{twilio_sid}",
            call_sid=twilio_sid,
            status=status,
        )
    )
    session.commit()


def test_normal_ai_end_hangs_up(session, client, business_a):
    _ai_leg(session, business_a, status="accepted")
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    assert "<Hangup" in body and "<Number>" not in body


@pytest.mark.parametrize("leg_status", ["handoff", "ai_failed"])
def test_ai_handoff_or_failure_transfers_to_the_tenants_person(
    session, client, business_a, leg_status
):
    _ai_leg(session, business_a, status=leg_status)
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    assert f"<Number>{RECEPTION_A}</Number>" in body
    assert "<Sip>" not in body  # never loops back into the AI


def test_ai_that_never_connected_transfers_to_a_person(client, business_a):
    body = _body(
        _post(
            client, "ivr/ai-complete", query="?route=AI_BOOKING&tried=AI", DialCallStatus="failed"
        )
    )
    assert f"<Number>{RECEPTION_A}</Number>" in body


def test_answered_transfer_hangs_up_when_done(client, business_a):
    body = _body(
        _post(client, "ivr/transfer-complete", query="?tried=HUMAN", DialCallStatus="completed")
    )
    assert "<Hangup" in body


def test_unanswered_transfer_offers_ai_when_that_is_the_fallback(session, client, business_a):
    settings = ivr_service.get_settings(business_a.id)
    settings.fallback_action = "AI_BOOKING"
    session.commit()
    body = _body(
        _post(client, "ivr/transfer-complete", query="?tried=HUMAN", DialCallStatus="no-answer")
    )
    assert "<Sip>" in body


def test_unanswered_transfer_with_nothing_left_logs_a_callback(client, business_a):
    body = _body(
        _post(client, "ivr/transfer-complete", query="?tried=HUMAN", DialCallStatus="no-answer")
    )
    assert "<Hangup" in body and "call you back" in body
    assert CallbackRequest.query.filter_by(garage_id=business_a.id).count() == 1


# --------------------------------------------------------------------------
# OpenAI webhook: signed handoff
# --------------------------------------------------------------------------


@pytest.fixture()
def openai_webhook(monkeypatch):
    monkeypatch.setattr(ai_voice_routes.gevent, "spawn", lambda fn: fn())
    controller = Mock()
    monkeypatch.setattr(ai_voice_routes, "run_call_controller", controller)
    accept = Mock()
    reject = Mock()
    monkeypatch.setattr(ai_voice_routes, "accept_call", accept)
    monkeypatch.setattr(ai_voice_routes, "reject_call", reject)
    return SimpleNamespace(controller=controller, accept=accept, reject=reject)


def _deliver(client, monkeypatch, call_id, sip_headers):
    event = SimpleNamespace(
        id="evt_1",
        type="realtime.call.incoming",
        data=SimpleNamespace(call_id=call_id, sip_headers=sip_headers),
    )
    monkeypatch.setattr(ai_voice_routes, "verify_webhook", Mock(return_value=event))
    return client.post("/api/webhooks/openai/realtime", data=b"{}")


def _signed_headers(app, garage, *, call_sid="CAivr0001", route="AI_BOOKING", now=None):
    with app.test_request_context():
        uri = sip_handoff.openai_sip_uri(
            garage_id=str(garage.id), twilio_call_sid=call_sid, route=route, caller=CALLER, now=now
        )
    query = urlsplit(uri.replace(";transport=tls?", "?")).query
    headers = [{"name": k, "value": unquote(v[0])} for k, v in parse_qs(query).items()]
    headers.append({"name": "To", "value": f"<sip:{PROJECT}@sip.api.openai.com;transport=tls>"})
    return headers


def test_signed_handoff_is_accepted_for_the_signed_tenant(
    app, client, business_a, monkeypatch, openai_webhook
):
    resp = _deliver(client, monkeypatch, "rtc_menu_1", _signed_headers(app, business_a))
    assert resp.status_code == 200
    openai_webhook.accept.assert_called_once()
    assert "chosen the bookings option" in openai_webhook.accept.call_args.kwargs["instructions"]

    leg = CommunicationLog.query.filter_by(external_id="rtc_menu_1").one()
    assert leg.garage_id == business_a.id
    assert leg.call_sid == "CAivr0001"
    kwargs = openai_webhook.controller.call_args.kwargs
    assert kwargs["ivr_bridged"] is True
    assert kwargs["garage"].id == business_a.id
    assert kwargs["caller_phone"] == CALLER


def test_tampered_handoff_is_rejected_not_downgraded(
    app, client, business_a, business_b, monkeypatch, openai_webhook
):
    headers = _signed_headers(app, business_a)
    for header in headers:
        if header["name"] == sip_handoff.HEADER_GARAGE:
            header["value"] = str(business_b.id)  # try to switch tenant
    _deliver(client, monkeypatch, "rtc_tampered", headers)
    openai_webhook.reject.assert_called_once()
    openai_webhook.accept.assert_not_called()
    assert CommunicationLog.query.filter_by(external_id="rtc_tampered").count() == 0


def test_expired_handoff_is_rejected(app, client, business_a, monkeypatch, openai_webhook):
    stale = time.time() - sip_handoff.MAX_AGE_SECONDS - 60
    _deliver(client, monkeypatch, "rtc_stale", _signed_headers(app, business_a, now=stale))
    openai_webhook.reject.assert_called_once()
    openai_webhook.accept.assert_not_called()


def test_replayed_handoff_cannot_open_a_second_ai_leg(
    app, client, business_a, monkeypatch, openai_webhook
):
    headers = _signed_headers(app, business_a)
    _deliver(client, monkeypatch, "rtc_first", headers)
    _deliver(client, monkeypatch, "rtc_replay", headers)
    assert openai_webhook.accept.call_count == 1
    openai_webhook.reject.assert_called_once()


def test_bridged_ai_leg_is_not_counted_as_a_second_call(session, business_a):
    session.add(
        CommunicationLog(
            garage_id=business_a.id,
            channel="VOICE",
            direction="INBOUND",
            external_provider="twilio",
            external_id="CAivr0001",
            status="in-progress",
        )
    )
    session.commit()
    _ai_leg(session, business_a, status="accepted")
    calls, total = queries.list_calls(business_a)
    assert total == 1 and calls[0].external_id == "CAivr0001"


# --------------------------------------------------------------------------
# Call controller in phone-menu mode
# --------------------------------------------------------------------------


class _Conn:
    def __init__(self, events):
        self._events = list(events)
        self.sent = []

    def recv(self):
        if not self._events:
            raise StopIteration
        return self._events.pop(0)

    def send_raw(self, data):
        self.sent.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_bridged_handoff_marks_the_leg_and_hangs_up_without_refer(session, business_a, monkeypatch):
    _ai_leg(session, business_a, status="accepted")
    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="h1",
            name="request_human_handoff",
            arguments='{"reason": "wants a person"}',
        ),
        SimpleNamespace(type="response.done"),
    ]
    client = Mock()
    client.realtime.connect.return_value = _Conn(events)
    monkeypatch.setattr(call_controller, "OpenAI", Mock(return_value=client))
    hangup, refer = Mock(), Mock()
    monkeypatch.setattr(call_controller, "hangup_call", hangup)
    monkeypatch.setattr(call_controller, "refer_call", refer)

    call_controller.run_call_controller(
        api_key="sk",
        call_id="rtc_CAivr0001",
        garage=business_a,
        caller_phone=CALLER,
        ivr_bridged=True,
    )

    hangup.assert_called_once_with("rtc_CAivr0001")
    refer.assert_not_called()
    db.session.expire_all()
    assert CommunicationLog.query.filter_by(external_id="rtc_CAivr0001").one().status == "handoff"


def test_bridged_ai_failure_marks_the_leg_so_the_menu_transfers(session, business_a, monkeypatch):
    _ai_leg(session, business_a, status="accepted")
    client = Mock()
    client.realtime.connect.side_effect = RuntimeError("You need to install `openai[realtime]`")
    monkeypatch.setattr(call_controller, "OpenAI", Mock(return_value=client))
    monkeypatch.setattr(call_controller, "RECONNECT_BACKOFF_SECONDS", (0, 0))
    hangup, refer = Mock(), Mock()
    monkeypatch.setattr(call_controller, "hangup_call", hangup)
    monkeypatch.setattr(call_controller, "refer_call", refer)

    call_controller.run_call_controller(
        api_key="sk",
        call_id="rtc_CAivr0001",
        garage=business_a,
        caller_phone=CALLER,
        ivr_bridged=True,
    )

    hangup.assert_called_once_with("rtc_CAivr0001")
    refer.assert_not_called()
    db.session.expire_all()
    assert CommunicationLog.query.filter_by(external_id="rtc_CAivr0001").one().status == "ai_failed"


# --------------------------------------------------------------------------
# Settings API (Settings > Phone menu)
# --------------------------------------------------------------------------

VALID_MENU = {
    "enabled": True,
    "greeting": "Thanks for calling Tints On Demand.",
    "options": [
        {"digit": "1", "label": "bookings", "action": "AI_BOOKING"},
        {"digit": "2", "label": "the team", "action": "HUMAN_TRANSFER", "target": "0161 222 3333"},
    ],
    "fallback_action": "HUMAN_TRANSFER",
    "max_attempts": 2,
}


def test_default_menu_is_off(authenticated_client, voice_config):
    data = authenticated_client.get("/api/communications/voice-menu").get_json()
    assert data["enabled"] is False and data["options"] == []
    assert {a["key"] for a in data["supported_actions"]} >= {
        "AI_BOOKING",
        "AI_FAQ",
        "HUMAN_TRANSFER",
        "REPEAT_MENU",
    }


def test_owner_can_save_a_menu_and_numbers_are_normalised(
    authenticated_client, garage, voice_config
):
    resp = authenticated_client.put("/api/communications/voice-menu", json=VALID_MENU)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["options"][1]["target"] == "+441612223333"
    assert ivr_service.get_settings(garage.id).options[0]["action"] == "AI_BOOKING"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"options": [{"digit": "1", "label": "a", "action": "AI_BOOKING"}] * 2}, "options"),
        ({"options": [{"digit": "#", "label": "a", "action": "AI_BOOKING"}]}, "options.0.digit"),
        (
            {"options": [{"digit": "1", "label": "a", "action": "DELETE_EVERYTHING"}]},
            "options.0.action",
        ),
        (
            {
                "options": [
                    {
                        "digit": "2",
                        "label": "x",
                        "action": "HUMAN_TRANSFER",
                        "target": "09011234567",
                    }
                ]
            },
            "options.0.target",
        ),
        (
            {
                "options": [
                    {
                        "digit": "2",
                        "label": "x",
                        "action": "HUMAN_TRANSFER",
                        "target": "+12125551234",
                    }
                ]
            },
            "options.0.target",
        ),
        (
            {
                "options": [
                    {"digit": "1", "label": "a", "action": "AI_BOOKING", "target": "01612223333"}
                ]
            },
            "options.0.target",
        ),
        ({"fallback_action": "REPEAT_MENU"}, "fallback_action"),
        ({"max_attempts": 9}, "max_attempts"),
        (
            {
                "options": [{"digit": "1", "label": "a", "action": "AI_BOOKING"}],
                "fallback_target": None,
            },
            "fallback_target",
        ),
    ],
)
def test_invalid_menus_are_rejected(authenticated_client, voice_config, change, field):
    resp = authenticated_client.put("/api/communications/voice-menu", json={**VALID_MENU, **change})
    assert resp.status_code == 422
    assert field in resp.get_json()["errors"]["json"]


def test_staff_cannot_change_the_menu(client, session, garage, staff_role, voice_config):
    staff = Employee(
        garage_id=garage.id,
        email="staff@garage-a.example",
        password_hash=generate_password_hash("Password123!"),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(staff.id))}"}
    assert client.get("/api/communications/voice-menu", headers=headers).status_code == 200
    resp = client.put("/api/communications/voice-menu", json=VALID_MENU, headers=headers)
    assert resp.status_code == 403


def test_menus_are_isolated_per_tenant(
    authenticated_client, second_authenticated_client, garage, second_garage, voice_config
):
    assert (
        authenticated_client.put("/api/communications/voice-menu", json=VALID_MENU).status_code
        == 200
    )
    other = second_authenticated_client.get("/api/communications/voice-menu").get_json()
    assert other["enabled"] is False and other["options"] == []
    assert ivr_service.get_settings(second_garage.id) is None
