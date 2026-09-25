"""Existing-number telephony (app/communications/telephony.py): SIP / BYOC and
call-forwarding ingress, tenant routing, the ordered human handoff chain,
loop prevention and the Platform Admin configuration that drives them.

TestConfig sets TWILIO_WEBHOOK_VALIDATE = False, so these tests drive the
Twilio webhooks directly with the form fields Twilio itself would send.
"""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.ai_voice import tools as ai_tools
from app.communications import queries, telephony
from app.communications.ivr import service as ivr_service
from app.models.communications.communication_log import CommunicationLog
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.communications.voice_ivr_settings import GarageVoiceIvrSettings
from app.models.conversation.callback_request import CallbackRequest

# Business A - an existing number kept with its carrier (SIP / BYOC).
PUBLIC_A = "+441619990001"
INGRESS_A = "+441611234567"
SD_A = "SD" + "a" * 32
BY_A = "BY" + "c" * 32
PBX_A = "sip:reception@pbx.example.co.uk"
MOBILE_A = "+447911123111"

# Business B - an existing number forwarded to a CoMaz ingress number.
PUBLIC_B = "+441619990002"
INGRESS_B = "+441619876543"
SD_B = "SD" + "b" * 32
DESK_B = "+441614445555"

CALLER = "+447700900123"
PROJECT = "proj_test123"
COMMS = "/api/platform-admin/tenants/{garage_id}/communications"


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


def _menu(session, garage, *, transfer_target=None):
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
                "label": "the team",
                "prompt": None,
                "action": "HUMAN_TRANSFER",
                "target": transfer_target,
            },
        ],
        fallback_action="HUMAN_TRANSFER",
        fallback_target=None,
        max_attempts=2,
    )
    session.add(settings)
    session.commit()
    return settings


@pytest.fixture()
def byoc_business(session, garage, voice_config):
    """SIP / BYOC: the carrier keeps PUBLIC_A and delivers it to SD_A. A PBX
    hunt group first, then the owner's mobile out through the carrier."""
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            communications_enabled=True,
            voice_phone_number=INGRESS_A,
            telephony_mode=telephony.MODE_SIP_BYOC,
            public_business_number=PUBLIC_A,
            byoc_sip_domain_sid=SD_A,
            byoc_trunk_sid=BY_A,
            human_primary_type=telephony.DEST_SIP_URI,
            human_primary_destination=PBX_A,
            human_secondary_type=telephony.DEST_PSTN_NUMBER,
            human_secondary_destination=MOBILE_A,
        )
    )
    session.commit()
    session.refresh(garage)
    _menu(session, garage)
    return garage


@pytest.fixture()
def forward_business(session, second_garage, voice_config):
    """Call forwarding: PUBLIC_B forwards to the CoMaz ingress INGRESS_B."""
    session.add(
        GarageCommunicationSettings(
            garage_id=second_garage.id,
            communications_enabled=True,
            voice_phone_number=INGRESS_B,
            telephony_mode=telephony.MODE_PSTN_FORWARD,
            public_business_number=PUBLIC_B,
            human_primary_type=telephony.DEST_PSTN_NUMBER,
            human_primary_destination=DESK_B,
        )
    )
    session.commit()
    session.refresh(second_garage)
    _menu(session, second_garage)
    return second_garage


def _post(client, step, *, call_sid="CAtel0001", query="", **form):
    data = {"From": CALLER, "CallSid": call_sid, **form}
    return client.post(f"/api/webhooks/twilio/voice/{step}{query}", data=data)


def _byoc_call(client, step="incoming", **extra):
    form = {"To": PUBLIC_A, "SipDomainSid": SD_A, **extra}
    return _post(client, step, **form)


def _body(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _action_query(body, marker="transfer-complete"):
    start = body.index(marker)
    end = body.index('"', start)
    return parse_qs(urlsplit(body[start:end].replace("&amp;", "&")).query)


def _sip_headers(body):
    start = body.index("<Sip>") + len("<Sip>")
    uri = body[start : body.index("</Sip>")].replace("&amp;", "&")
    query = urlsplit(uri.replace(";transport=tls?", "?")).query
    return {k: unquote(v[0]) for k, v in parse_qs(query, keep_blank_values=True).items()}


def _ai_leg(session, garage, call_sid, *, status, body=None):
    session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel="VOICE",
            direction="INBOUND",
            external_provider="openai",
            external_id=f"rtc_{call_sid}",
            call_sid=call_sid,
            status=status,
            body=body,
        )
    )
    session.commit()


def _transfer_rows(garage, call_sid="CAtel0001"):
    return (
        CommunicationLog.query.filter_by(
            garage_id=garage.id, external_provider=telephony.TRANSFER_PROVIDER, call_sid=call_sid
        )
        .order_by(CommunicationLog.created_at)
        .all()
    )


# --------------------------------------------------------------------------
# SIP / BYOC ingress - tenant routing
# --------------------------------------------------------------------------


def test_sip_byoc_call_resolves_to_the_business_on_its_own_domain(client, byoc_business):
    body = _body(_byoc_call(client))
    assert f"Welcome to {byoc_business.name}." in body
    assert "<Gather" in body


@pytest.mark.parametrize(
    "to",
    [PUBLIC_A, "sip:+441619990001@comaz.sip.twilio.com", "441619990001", "01619990001"],
)
def test_sip_byoc_called_number_formats_all_resolve(client, byoc_business, to):
    body = _body(_post(client, "incoming", To=to, SipDomainSid=SD_A))
    assert f"Welcome to {byoc_business.name}." in body


def test_sip_call_on_the_wrong_domain_fails_closed(client, byoc_business):
    body = _body(_post(client, "incoming", To=PUBLIC_A, SipDomainSid=SD_B))
    assert "not currently in service" in body
    assert "<Gather" not in body


def test_sip_call_cannot_pick_another_tenant_with_its_number(
    client, byoc_business, forward_business
):
    # B's forwarded public number, but arriving on A's SIP domain: neither
    # business matches both - refused rather than guessed.
    body = _body(_post(client, "incoming", To=PUBLIC_B, SipDomainSid=SD_A))
    assert "not currently in service" in body


def test_sip_call_to_a_comaz_ingress_number_is_not_a_byoc_call(client, byoc_business):
    body = _body(_post(client, "incoming", To=INGRESS_A, SipDomainSid=SD_A))
    assert "not currently in service" in body


def test_pstn_call_to_a_byoc_public_number_does_not_resolve(client, byoc_business):
    # Without SipDomainSid a call can only be for a number CoMaz owns.
    body = _body(_post(client, "incoming", To=PUBLIC_A))
    assert "not currently in service" in body


def test_sip_byoc_menu_steps_stay_pinned_to_the_tenant(client, byoc_business):
    _body(_byoc_call(client))
    # Twilio's action callback need not repeat SipDomainSid.
    body = _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="1"))
    assert "<Sip>" in body
    assert _sip_headers(body)["X-CoMaz-Garage"] == str(byoc_business.id)


def test_a_call_step_whose_request_names_another_tenant_is_refused(
    client, byoc_business, forward_business
):
    _body(_byoc_call(client))
    body = _body(_post(client, "ivr/menu", To=INGRESS_B, query="?attempt=1", Digits="2"))
    assert "not currently in service" in body


# --------------------------------------------------------------------------
# SIP / BYOC - AI success and human handoff
# --------------------------------------------------------------------------


def test_sip_byoc_ai_option_bridges_with_the_real_caller(client, byoc_business):
    _body(_byoc_call(client))
    body = _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="1"))
    headers = _sip_headers(body)
    assert headers["X-CoMaz-Garage"] == str(byoc_business.id)
    assert headers["X-CoMaz-Caller"] == CALLER.lstrip("+")


def test_normal_ai_end_hangs_up_without_a_transfer(session, client, byoc_business):
    _body(_byoc_call(client))
    _ai_leg(session, byoc_business, "CAtel0001", status="accepted")
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    assert "<Hangup" in body and "<Dial" not in body
    assert _transfer_rows(byoc_business) == []


def test_caller_asking_for_a_person_goes_to_the_pbx_over_sip(session, client, byoc_business):
    _body(_byoc_call(client))
    _ai_leg(session, byoc_business, "CAtel0001", status="handoff", body="Wants a quote")
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    assert f"<Sip>{PBX_A}</Sip>" in body
    assert "Putting you through now." in body
    rows = _transfer_rows(byoc_business)
    assert [(r.to_address, r.status) for r in rows] == [(PBX_A, telephony.TRANSFER_DIALLING)]


def test_ai_that_fails_mid_call_hands_off_to_a_person(session, client, byoc_business):
    _body(_byoc_call(client))
    _ai_leg(session, byoc_business, "CAtel0001", status="ai_failed")
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    assert f"<Sip>{PBX_A}</Sip>" in body


def test_ai_that_never_answers_hands_off_to_a_person(client, byoc_business):
    _body(_byoc_call(client))
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="failed",
        )
    )
    assert f"<Sip>{PBX_A}</Sip>" in body


def test_sip_handoff_no_answer_moves_to_the_secondary_through_the_carrier(
    session, client, byoc_business
):
    _body(_byoc_call(client))
    _ai_leg(session, byoc_business, "CAtel0001", status="handoff", body="Wants a quote")
    first = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    query = _action_query(first)
    assert query["hi"] == ["1"]

    second = _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=PUBLIC_A,
            query=f"?tried={query['tried'][0]}&hi=1",
            DialCallStatus="no-answer",
        )
    )
    # Out through the business's own carrier, not CoMaz's PSTN route.
    assert f'<Number byoc="{BY_A}">{MOBILE_A}</Number>' in second
    assert "Trying someone else now." in second
    assert "connected" not in second.lower()
    statuses = [(r.to_address, r.status) for r in _transfer_rows(byoc_business)]
    assert statuses == [(PBX_A, "no-answer"), (MOBILE_A, telephony.TRANSFER_DIALLING)]


def test_sip_rejection_then_no_answer_ends_in_one_callback_with_the_ai_reason(
    session, client, byoc_business
):
    _body(_byoc_call(client))
    _ai_leg(session, byoc_business, "CAtel0001", status="handoff", body="Wants a quote")
    _body(
        _post(
            client,
            "ivr/ai-complete",
            To=PUBLIC_A,
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="completed",
        )
    )
    _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=PUBLIC_A,
            query="?tried=AI,HUMAN&hi=1",
            DialCallStatus="failed",
            DialSipResponseCode="503",
        )
    )
    final = _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=PUBLIC_A,
            query="?tried=AI,HUMAN&hi=2",
            DialCallStatus="busy",
        )
    )
    assert "<Dial" not in final
    assert "call you back" in final and "<Hangup" in final

    callbacks = CallbackRequest.query.filter_by(garage_id=byoc_business.id).all()
    assert len(callbacks) == 1
    assert "Wants a quote" in callbacks[0].reason
    rows = _transfer_rows(byoc_business)
    assert [r.status for r in rows] == ["failed", "busy"]
    assert rows[0].error_code == "503"


def test_the_chain_is_finite_even_if_asked_for_more(client, byoc_business):
    _body(_byoc_call(client))
    body = _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=PUBLIC_A,
            query="?tried=HUMAN&hi=7",
            DialCallStatus="no-answer",
        )
    )
    assert "<Dial" not in body and "<Hangup" in body


def test_answered_transfer_is_recorded_and_ends_cleanly(session, client, byoc_business):
    _body(_byoc_call(client))
    _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="2"))
    body = _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=PUBLIC_A,
            query="?tried=HUMAN&hi=1",
            DialCallStatus="completed",
            DialCallSid="CAchild0001",
        )
    )
    assert "<Hangup" in body and "<Dial" not in body
    [row] = _transfer_rows(byoc_business)
    assert (row.status, row.external_id) == ("completed", "CAchild0001")
    assert CallbackRequest.query.filter_by(garage_id=byoc_business.id).count() == 0


def test_a_redelivered_transfer_report_changes_nothing(session, client, byoc_business):
    _body(_byoc_call(client))
    _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="2"))
    for _ in range(2):
        _body(
            _post(
                client,
                "ivr/transfer-complete",
                To=PUBLIC_A,
                query="?tried=HUMAN&hi=1",
                DialCallStatus="no-answer",
            )
        )
    rows = _transfer_rows(byoc_business)
    # One row per attempt: the primary (no-answer) and the secondary, which a
    # repeated report neither re-records nor marks as unanswered.
    assert [(r.to_address, r.status) for r in rows] == [
        (PBX_A, "no-answer"),
        (MOBILE_A, telephony.TRANSFER_DIALLING),
    ]


def test_parent_call_ending_closes_a_transfer_still_ringing(session, client, byoc_business):
    _body(_byoc_call(client))
    _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="2"))
    resp = client.post(
        "/api/webhooks/twilio/voice/status",
        data={"CallSid": "CAtel0001", "CallStatus": "completed", "CallDuration": "40"},
    )
    assert resp.status_code == 204
    [row] = _transfer_rows(byoc_business)
    assert row.status == telephony.TRANSFER_ENDED


def test_transfer_attempts_are_not_counted_as_calls(session, client, byoc_business):
    _body(_byoc_call(client))
    _body(_post(client, "ivr/menu", To=PUBLIC_A, query="?attempt=1", Digits="2"))
    calls, total = queries.list_calls(byoc_business)
    assert total == 1
    assert calls[0].external_provider == "twilio"


# --------------------------------------------------------------------------
# PSTN call forwarding
# --------------------------------------------------------------------------


def test_forwarded_call_resolves_by_the_comaz_ingress(client, forward_business):
    body = _body(
        _post(client, "incoming", To=INGRESS_B, ForwardedFrom=PUBLIC_B, call_sid="CAfwd0001")
    )
    assert f"Welcome to {forward_business.name}." in body


def test_forwarded_call_reaches_the_ai_with_the_real_caller(client, forward_business):
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0001"))
    body = _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0001",
            query="?attempt=1",
            Digits="1",
        )
    )
    headers = _sip_headers(body)
    assert headers["X-CoMaz-Garage"] == str(forward_business.id)
    assert headers["X-CoMaz-Caller"] == CALLER.lstrip("+")


def test_forwarded_call_hands_off_to_the_desk_not_the_public_number(client, forward_business):
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0001"))
    body = _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0001",
            query="?attempt=1",
            Digits="2",
        )
    )
    assert f"<Number>{DESK_B}</Number>" in body
    assert PUBLIC_B not in body
    # Not a BYOC business: no carrier egress attribute.
    assert "byoc=" not in body


def test_forwarding_line_presented_as_caller_is_not_treated_as_a_customer(client, forward_business):
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0002", From=PUBLIC_B))
    body = _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0002",
            From=PUBLIC_B,
            query="?attempt=1",
            Digits="1",
        )
    )
    assert _sip_headers(body)["X-CoMaz-Caller"] == ""


def test_withheld_caller_gets_no_callback_record_but_a_polite_close(
    session, client, forward_business
):
    comm = forward_business.communication_settings
    comm.human_primary_type = None
    comm.human_primary_destination = None
    session.commit()
    ivr_service.get_settings(forward_business.id).options = [
        {"digit": "1", "label": "bookings", "prompt": None, "action": "AI_BOOKING", "target": None}
    ]
    session.commit()
    body = _body(
        _post(
            client,
            "ivr/ai-complete",
            To=INGRESS_B,
            From="anonymous",
            call_sid="CAfwd0003",
            query="?route=AI_BOOKING&tried=AI",
            DialCallStatus="failed",
        )
    )
    assert "<Hangup" in body
    assert CallbackRequest.query.filter_by(garage_id=forward_business.id).count() == 0


# --------------------------------------------------------------------------
# Loop prevention
# --------------------------------------------------------------------------


def test_a_saved_destination_that_rings_comaz_is_never_dialled(session, client, forward_business):
    # A menu option pointing at the business's own forwarded public number -
    # saved before the rule existed - is skipped at dial time.
    settings = ivr_service.get_settings(forward_business.id)
    options = list(settings.options)
    options[1] = {**options[1], "target": PUBLIC_B}
    settings.options = options
    session.commit()
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0004"))
    body = _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0004",
            query="?attempt=1",
            Digits="2",
        )
    )
    assert PUBLIC_B not in body
    assert f"<Number>{DESK_B}</Number>" in body


def test_another_tenants_comaz_number_is_never_dialled(
    session, client, byoc_business, forward_business
):
    comm = forward_business.communication_settings
    comm.human_primary_destination = INGRESS_A
    session.commit()
    chain = telephony.human_destinations(forward_business)
    assert INGRESS_A not in [d.value for d in chain]


def test_a_transfer_ringing_comaz_again_is_refused_as_busy(client, forward_business):
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0005"))
    _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0005",
            query="?attempt=1",
            Digits="2",
        )
    )
    # The desk forwards back to the public number: the same caller arrives
    # again while the first transfer is still ringing.
    looped = _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0006"))
    assert '<Reject reason="busy"' in looped
    assert "<Dial" not in looped


def test_the_same_caller_can_call_again_once_the_transfer_has_ended(client, forward_business):
    _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0007"))
    _body(
        _post(
            client,
            "ivr/menu",
            To=INGRESS_B,
            call_sid="CAfwd0007",
            query="?attempt=1",
            Digits="2",
        )
    )
    _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=INGRESS_B,
            call_sid="CAfwd0007",
            query="?tried=HUMAN&hi=1",
            DialCallStatus="completed",
        )
    )
    body = _body(_post(client, "incoming", To=INGRESS_B, call_sid="CAfwd0008"))
    assert "<Gather" in body


def test_owner_cannot_save_a_menu_transfer_to_their_own_forwarded_number(
    authenticated_client, session, garage, voice_config
):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            voice_phone_number=INGRESS_A,
            telephony_mode=telephony.MODE_PSTN_FORWARD,
            public_business_number=PUBLIC_A,
        )
    )
    session.commit()
    resp = authenticated_client.put(
        "/api/communications/voice-menu",
        json={
            "enabled": True,
            "options": [
                {
                    "digit": "2",
                    "label": "reception",
                    "action": "HUMAN_TRANSFER",
                    "target": "01619990001",
                }
            ],
            "fallback_action": "HUMAN_TRANSFER",
            "max_attempts": 2,
        },
    )
    assert resp.status_code == 422
    assert "loop" in json.dumps(resp.get_json()).lower()


# --------------------------------------------------------------------------
# Direct-trunk REFER target and the AI tool contract
# --------------------------------------------------------------------------


def test_direct_trunk_refer_uses_the_primary_sip_destination(byoc_business):
    assert ai_tools._fallback_transfer_uri(byoc_business) == PBX_A


def test_direct_trunk_refer_uses_tel_for_a_pstn_primary(forward_business):
    assert ai_tools._fallback_transfer_uri(forward_business) == f"tel:{DESK_B}"


def test_bridged_handoff_logs_no_callback_until_nobody_answers(byoc_business):
    result = json.loads(
        ai_tools.dispatch_tool(
            byoc_business,
            CALLER,
            "request_human_handoff",
            json.dumps({"reason": "Wants a quote"}),
            ivr_bridged=True,
        )
    )
    assert result == {"ok": True, "status": "transferring"}
    assert CallbackRequest.query.filter_by(garage_id=byoc_business.id).count() == 0


def test_direct_handoff_still_logs_a_callback_and_refers(byoc_business):
    result = json.loads(
        ai_tools.dispatch_tool(
            byoc_business,
            CALLER,
            "request_human_handoff",
            json.dumps({"reason": "Wants a quote"}),
        )
    )
    assert result["ok"] is True and result["_transfer_uri"] == PBX_A
    assert CallbackRequest.query.filter_by(garage_id=byoc_business.id).count() == 1


def test_handoff_tool_never_promises_a_connection():
    description = next(
        t["description"] for t in ai_tools.TOOL_SCHEMAS if t["name"] == "request_human_handoff"
    )
    assert "never say they are connected" in description


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "sip:reception@comaz.sip.twilio.com",
        f"sip:{PROJECT}@sip.api.openai.com",
        "sip:reception@10.0.0.5",
        "sip:reception@127.0.0.1",
        "sip:reception@pbx.example.co.uk?X-Evil=1",
        "sip:reception@pbx.example.co.uk;transport=tls;maddr=1.2.3.4",
        "tel:+441612223333",
        "reception@pbx.example.co.uk",
        "sip:reception@localhost",
    ],
)
def test_unsafe_sip_destinations_are_rejected(uri):
    with pytest.raises(telephony.TelephonyValidationError):
        telephony.validate_sip_destination(uri, field="d")


@pytest.mark.parametrize(
    "uri",
    [PBX_A, "sips:6001@pbx.example.co.uk:5061;transport=tls", "sip:+441612223333@81.2.69.160"],
)
def test_real_pbx_destinations_are_accepted(uri):
    assert telephony.validate_sip_destination(uri, field="d") == uri


@pytest.mark.parametrize("number", ["09012345678", "+12125551234", "not a number"])
def test_unsafe_pstn_destinations_are_rejected(app, number):
    with pytest.raises(telephony.TelephonyValidationError):
        telephony.validate_pstn_destination(number, field="d")


# --------------------------------------------------------------------------
# Platform Admin
# --------------------------------------------------------------------------


@pytest.fixture()
def comms(session, garage):
    settings = GarageCommunicationSettings(
        garage_id=garage.id, communications_enabled=True, voice_phone_number=INGRESS_A
    )
    session.add(settings)
    session.commit()
    session.refresh(garage)
    return settings


def _put(platform_client, garage, **payload):
    return platform_client.put(f"{COMMS.format(garage_id=garage.id)}/voice/telephony", json=payload)


def test_admin_configures_sip_byoc_with_a_pbx_primary(platform_client, session, garage, comms):
    resp = _put(
        platform_client,
        garage,
        telephony_mode="SIP_BYOC",
        public_business_number="0161 999 0001",
        byoc_sip_domain_sid=SD_A,
        byoc_trunk_sid=BY_A,
        human_primary_type="SIP_URI",
        human_primary_destination=PBX_A,
        human_secondary_type="PSTN_NUMBER",
        human_secondary_destination="07911 123111",
        human_transfer_timeout_seconds=20,
    )
    assert resp.status_code == 200, resp.get_json()
    view = resp.get_json()["voice"]["telephony"]
    assert view["telephony_mode"] == "SIP_BYOC"
    assert view["public_business_number"] == PUBLIC_A
    assert view["comaz_ingress_number"] == INGRESS_A
    assert view["human_transfer_timeout_seconds"] == 20
    assert [e["destination"] for e in view["effective_human_chain"]] == [PBX_A, MOBILE_A]
    session.refresh(comms)
    assert comms.human_secondary_destination == MOBILE_A


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"telephony_mode": "PORTED"}, None),
        (
            {
                "telephony_mode": "SIP_BYOC",
                "byoc_sip_domain_sid": SD_A,
                "human_primary_type": "SIP_URI",
                "human_primary_destination": PBX_A,
            },
            "public_business_number",
        ),
        (
            {
                "telephony_mode": "SIP_BYOC",
                "public_business_number": PUBLIC_A,
                "human_primary_type": "SIP_URI",
                "human_primary_destination": PBX_A,
            },
            "byoc_sip_domain_sid",
        ),
        (
            {
                "telephony_mode": "SIP_BYOC",
                "public_business_number": PUBLIC_A,
                "byoc_sip_domain_sid": SD_A,
            },
            "human_primary_destination",
        ),
        (
            {
                "telephony_mode": "SIP_BYOC",
                "public_business_number": PUBLIC_A,
                "byoc_sip_domain_sid": "SDnope",
                "human_primary_type": "SIP_URI",
                "human_primary_destination": PBX_A,
            },
            "byoc_sip_domain_sid",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": INGRESS_A,
                "human_primary_type": "PSTN_NUMBER",
                "human_primary_destination": DESK_B,
            },
            "public_business_number",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": PUBLIC_A,
                "human_primary_type": "PSTN_NUMBER",
                "human_primary_destination": PUBLIC_A,
            },
            "human_primary_destination",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": PUBLIC_A,
                "human_primary_type": "PSTN_NUMBER",
                "human_primary_destination": INGRESS_A,
            },
            "human_primary_destination",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": PUBLIC_A,
                "human_primary_type": "SIP_URI",
                "human_primary_destination": "sip:desk@comaz.sip.twilio.com",
            },
            "human_primary_destination",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": PUBLIC_A,
                "human_secondary_type": "PSTN_NUMBER",
                "human_secondary_destination": DESK_B,
            },
            "human_primary_destination",
        ),
        (
            {
                "telephony_mode": "PSTN_FORWARD",
                "public_business_number": PUBLIC_A,
                "human_primary_type": "PSTN_NUMBER",
                "human_primary_destination": DESK_B,
                "human_transfer_timeout_seconds": 300,
            },
            "human_transfer_timeout_seconds",
        ),
        (
            {"telephony_mode": "NEW_COMAZ_NUMBER", "public_business_number": PUBLIC_A},
            "public_business_number",
        ),
        (
            {"telephony_mode": "NEW_COMAZ_NUMBER", "byoc_trunk_sid": BY_A},
            "byoc_trunk_sid",
        ),
    ],
)
def test_admin_rejects_invalid_telephony(platform_client, garage, comms, payload, field):
    resp = _put(platform_client, garage, **payload)
    assert resp.status_code == 422, resp.get_json()
    if field:
        assert resp.get_json()["errors"]["field"] == field


def test_admin_rejects_a_public_number_another_business_owns(
    platform_client, session, garage, comms, forward_business
):
    resp = _put(
        platform_client,
        garage,
        telephony_mode="PSTN_FORWARD",
        public_business_number=PUBLIC_B,
        human_primary_type="PSTN_NUMBER",
        human_primary_destination=MOBILE_A,
    )
    assert resp.status_code == 422


def test_admin_rejects_another_tenants_number_as_a_destination(
    platform_client, garage, comms, forward_business
):
    resp = _put(
        platform_client,
        garage,
        telephony_mode="NEW_COMAZ_NUMBER",
        human_primary_type="PSTN_NUMBER",
        human_primary_destination=INGRESS_B,
    )
    assert resp.status_code == 422
    assert resp.get_json()["errors"]["field"] == "human_primary_destination"


def test_forward_mode_needs_a_comaz_ingress_first(platform_client, session, garage):
    session.add(GarageCommunicationSettings(garage_id=garage.id))
    session.commit()
    resp = _put(
        platform_client,
        garage,
        telephony_mode="PSTN_FORWARD",
        public_business_number=PUBLIC_A,
        human_primary_type="PSTN_NUMBER",
        human_primary_destination=MOBILE_A,
    )
    assert resp.status_code == 422
    assert resp.get_json()["errors"]["field"] == "telephony_mode"


def test_support_admins_cannot_change_telephony(support_client, garage, comms):
    resp = support_client.put(
        f"{COMMS.format(garage_id=garage.id)}/voice/telephony",
        json={"telephony_mode": "NEW_COMAZ_NUMBER"},
    )
    assert resp.status_code == 403


def test_telephony_changes_are_audited(platform_client, garage, comms):
    from app.models.platform.audit_log import PlatformAuditLog

    resp = _put(
        platform_client,
        garage,
        telephony_mode="PSTN_FORWARD",
        public_business_number=PUBLIC_A,
        human_primary_type="PSTN_NUMBER",
        human_primary_destination=MOBILE_A,
    )
    assert resp.status_code == 200
    entry = PlatformAuditLog.query.order_by(PlatformAuditLog.created_at.desc()).first()
    assert "PSTN_FORWARD" in entry.summary


def test_an_unconfigured_business_keeps_its_legacy_escalation(session, garage, voice_config):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            communications_enabled=True,
            voice_phone_number=INGRESS_A,
            voice_escalation_number=DESK_B,
        )
    )
    session.commit()
    session.refresh(garage)
    view = telephony.describe(garage)
    assert view["telephony_mode"] == telephony.MODE_NEW_COMAZ_NUMBER
    assert view["telephony_mode_configured"] is False
    assert [e["destination"] for e in view["effective_human_chain"]] == [DESK_B]


def test_no_menu_business_rings_its_person_with_a_safe_ending(
    client, session, garage, voice_config
):
    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            communications_enabled=True,
            voice_phone_number=INGRESS_A,
            voice_escalation_number=DESK_B,
        )
    )
    session.commit()
    body = _body(_post(client, "incoming", To=INGRESS_A, call_sid="CAnomenu1"))
    assert f"<Number>{DESK_B}</Number>" in body
    assert "/ivr/transfer-complete" in body
    # Nobody answers: a logged callback, never a dropped caller.
    final = _body(
        _post(
            client,
            "ivr/transfer-complete",
            To=INGRESS_A,
            call_sid="CAnomenu1",
            query="?tried=HUMAN&hi=1",
            DialCallStatus="no-answer",
        )
    )
    assert "call you back" in final
    assert CallbackRequest.query.filter_by(garage_id=garage.id).count() == 1
