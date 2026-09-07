"""API tests for the Communications Automation endpoints built on top of the
conversation engine:

GET  /api/communications/automation-settings
PUT  /api/communications/automation-settings
GET  /api/communications/templates
PUT  /api/communications/templates/<key>
DELETE /api/communications/templates/<key>
POST /api/communications/templates/<key>/preview
GET  /api/communications/callback-requests
POST /api/communications/callback-requests/<id>/complete
POST /api/communications/callback-requests/<id>/cancel
GET  /api/communications/conversations/<phone>/automation
POST /api/communications/conversations/<phone>/takeover
POST /api/communications/conversations/<phone>/resume-automation

None of these expose or accept a Twilio credential - that stays
platform/CLI-only (app/garages/details.py, app/communications/cli.py).
"""

from datetime import UTC, datetime

from app.conversation import engine
from app.models.conversation.callback_request import CallbackRequest
from app.models.conversation.conversation_session import ConversationSession

PHONE_RAW = "+447123400500"


# --------------------------------------------------------------------------
# Automation settings
# --------------------------------------------------------------------------


def test_automation_settings_default_to_safe_off(authenticated_client):
    resp = authenticated_client.get("/api/communications/automation-settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["conversation_automation_enabled"] is False
    assert body["booking_ack_enabled"] is True
    assert "twilio" not in str(body).lower()


def test_automation_settings_put_partial_update_only_changes_given_fields(authenticated_client):
    resp = authenticated_client.put(
        "/api/communications/automation-settings", json={"conversation_automation_enabled": True}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["conversation_automation_enabled"] is True
    assert body["booking_ack_enabled"] is True  # untouched, still its default

    resp2 = authenticated_client.get("/api/communications/automation-settings")
    assert resp2.get_json()["conversation_automation_enabled"] is True


def test_automation_settings_are_tenant_scoped(authenticated_client, second_authenticated_client):
    authenticated_client.put(
        "/api/communications/automation-settings", json={"conversation_automation_enabled": True}
    )
    resp = second_authenticated_client.get("/api/communications/automation-settings")
    assert resp.get_json()["conversation_automation_enabled"] is False


# --------------------------------------------------------------------------
# Message templates
# --------------------------------------------------------------------------


def test_templates_list_defaults_to_built_in_wording(authenticated_client):
    resp = authenticated_client.get("/api/communications/templates")
    assert resp.status_code == 200
    items = {item["key"]: item for item in resp.get_json()["items"]}
    assert "booking_acknowledgement" in items
    assert items["booking_acknowledgement"]["is_custom"] is False
    assert (
        items["booking_acknowledgement"]["body"] == items["booking_acknowledgement"]["default_body"]
    )


def test_template_put_saves_a_custom_override(authenticated_client):
    resp = authenticated_client.put(
        "/api/communications/templates/booking_acknowledgement",
        json={"body": "Cheers {{customer_first_name}}, booked in!"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["is_custom"] is True
    assert body["body"] == "Cheers {{customer_first_name}}, booked in!"

    listed = authenticated_client.get("/api/communications/templates").get_json()["items"]
    updated = next(i for i in listed if i["key"] == "booking_acknowledgement")
    assert updated["is_custom"] is True


def test_template_delete_reverts_to_default(authenticated_client):
    authenticated_client.put(
        "/api/communications/templates/booking_acknowledgement", json={"body": "Custom text"}
    )
    resp = authenticated_client.delete("/api/communications/templates/booking_acknowledgement")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["is_custom"] is False
    assert body["body"] == body["default_body"]


def test_template_unknown_key_404s(authenticated_client):
    resp = authenticated_client.put(
        "/api/communications/templates/not_a_real_key", json={"body": "hi"}
    )
    assert resp.status_code == 404


def test_template_preview_substitutes_sample_values_never_saves(authenticated_client):
    resp = authenticated_client.post(
        "/api/communications/templates/booking_acknowledgement/preview",
        json={"body": "Hi {{customer_first_name}}, thanks for booking with {{business_name}}."},
    )
    assert resp.status_code == 200
    preview = resp.get_json()["preview"]
    assert "{{" not in preview
    assert "Jane" in preview

    listed = authenticated_client.get("/api/communications/templates").get_json()["items"]
    unchanged = next(i for i in listed if i["key"] == "booking_acknowledgement")
    assert unchanged["is_custom"] is False  # preview never persists anything


# --------------------------------------------------------------------------
# Callback requests
# --------------------------------------------------------------------------


def _make_callback(session, garage, **overrides):
    fields = {"garage_id": garage.id, "phone_number": PHONE_RAW, "reason": "car won't start"}
    fields.update(overrides)
    row = CallbackRequest(**fields)
    session.add(row)
    session.commit()
    return row


def test_callback_requests_list_defaults_to_all_statuses(session, garage, authenticated_client):
    _make_callback(session, garage)
    _make_callback(session, garage, status="COMPLETED")

    resp = authenticated_client.get("/api/communications/callback-requests")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 2


def test_callback_requests_list_filters_by_status(session, garage, authenticated_client):
    _make_callback(session, garage)
    _make_callback(session, garage, status="COMPLETED")

    resp = authenticated_client.get("/api/communications/callback-requests?status=PENDING")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "PENDING"


def test_callback_request_complete_marks_it_done(session, garage, authenticated_client):
    callback = _make_callback(session, garage)

    resp = authenticated_client.post(
        f"/api/communications/callback-requests/{callback.id}/complete"
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "COMPLETED"
    session.refresh(callback)
    assert callback.status == "COMPLETED"


def test_callback_request_cancel_marks_it_cancelled(session, garage, authenticated_client):
    callback = _make_callback(session, garage)

    resp = authenticated_client.post(f"/api/communications/callback-requests/{callback.id}/cancel")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "CANCELLED"


def test_callback_request_404s_for_other_tenants_row(
    session, garage, second_garage, authenticated_client
):
    callback = _make_callback(session, second_garage)
    resp = authenticated_client.post(
        f"/api/communications/callback-requests/{callback.id}/complete"
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Conversation automation status / takeover / resume-automation
# --------------------------------------------------------------------------


def _now():
    return datetime(2026, 9, 7, 8, 0, tzinfo=UTC)  # a Monday


def test_automation_status_with_no_session_is_null(authenticated_client):
    resp = authenticated_client.get(f"/api/communications/conversations/{PHONE_RAW}/automation")
    assert resp.status_code == 200
    assert resp.get_json()["status"] is None


def test_takeover_with_no_session_404s(authenticated_client):
    resp = authenticated_client.post(f"/api/communications/conversations/{PHONE_RAW}/takeover")
    assert resp.status_code == 404


def test_takeover_hands_an_active_engine_session_to_a_human(
    garage, garage_schedule, appointment_type, user, authenticated_client
):
    # A real in-progress bot conversation, driven through the actual engine -
    # not a hand-built row - so this proves the API acts on the same session
    # the engine itself would find on the customer's next message.
    r1 = engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="I need an MOT", now=_now()
    )
    assert r1.workflow_step is not None  # a real in-progress flow, not an immediate handoff

    resp = authenticated_client.post(f"/api/communications/conversations/{PHONE_RAW}/takeover")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "HUMAN_HANDOFF"
    assert "Taken over by" in body["handoff_reason"]

    conv_session = ConversationSession.query.filter_by(
        garage_id=garage.id, customer_phone=PHONE_RAW
    ).one()
    assert conv_session.status == "HUMAN_HANDOFF"

    # The engine must not reply automatically to the next message either.
    result = engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="hello?", now=_now()
    )
    assert result.needs_human is True
    assert result.response_text is None


def test_resume_automation_requires_a_handed_off_session(
    garage, garage_schedule, appointment_type, user, authenticated_client
):
    r1 = engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="I need an MOT", now=_now()
    )
    assert r1.workflow_step is not None  # a real in-progress flow, not an immediate handoff

    resp = authenticated_client.post(
        f"/api/communications/conversations/{PHONE_RAW}/resume-automation"
    )
    assert resp.status_code == 404  # still ACTIVE, nothing to resume


def test_resume_automation_clears_the_handoff(garage, authenticated_client):
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="speak to a human", now=_now()
    )
    assert (
        ConversationSession.query.filter_by(garage_id=garage.id, customer_phone=PHONE_RAW)
        .one()
        .status
        == "HUMAN_HANDOFF"
    )

    resp = authenticated_client.post(
        f"/api/communications/conversations/{PHONE_RAW}/resume-automation"
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ACTIVE"
    assert resp.get_json()["handoff_reason"] is None


def test_attention_queue_lists_only_human_handoff_conversations(
    garage, garage_schedule, appointment_type, user, authenticated_client
):
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="I need an MOT", now=_now()
    )
    other_phone = "+447123400600"
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=other_phone, text="speak to a human", now=_now()
    )

    resp = authenticated_client.get("/api/communications/attention-queue")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["phone"] == other_phone
    assert items[0]["handoff_reason"]


def test_attention_queue_empties_once_resumed(garage, authenticated_client):
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="speak to a human", now=_now()
    )
    assert (
        len(authenticated_client.get("/api/communications/attention-queue").get_json()["items"])
        == 1
    )

    authenticated_client.post(f"/api/communications/conversations/{PHONE_RAW}/resume-automation")

    assert authenticated_client.get("/api/communications/attention-queue").get_json()["items"] == []


def test_attention_queue_is_tenant_scoped(
    garage, second_garage, authenticated_client, second_authenticated_client
):
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="speak to a human", now=_now()
    )

    resp = second_authenticated_client.get("/api/communications/attention-queue")
    assert resp.get_json()["items"] == []


def test_conversation_automation_endpoints_are_tenant_scoped(
    garage,
    garage_schedule,
    appointment_type,
    user,
    second_garage,
    authenticated_client,
    second_authenticated_client,
):
    engine.handle_message(
        garage, channel="WHATSAPP", phone_e164=PHONE_RAW, text="I need an MOT", now=_now()
    )

    resp = second_authenticated_client.get(
        f"/api/communications/conversations/{PHONE_RAW}/automation"
    )
    assert resp.get_json()["status"] is None  # second garage never sees the first garage's session

    resp = second_authenticated_client.post(
        f"/api/communications/conversations/{PHONE_RAW}/takeover"
    )
    assert resp.status_code == 404
