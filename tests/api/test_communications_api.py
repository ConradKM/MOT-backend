"""API tests for the garage-facing Communications endpoints:

GET  /api/communications/overview
GET  /api/communications/unread-count
GET  /api/communications/calls
POST /api/communications/calls
GET  /api/communications/calls/<id>
GET  /api/communications/conversations
GET  /api/communications/conversations/<phone>/messages
POST /api/communications/conversations/<phone>/read
POST /api/communications/whatsapp/send
GET  /api/customers/<id>/communications

None of these need Twilio configured - the whole suite runs without it
(TestConfig leaves TWILIO_ACCOUNT_SID/TOKEN unset), the same as every other
test file.
"""

from datetime import UTC, datetime, timedelta

from app.models.communications.communication_log import CommunicationLog

CALLER = "+447123400111"
UNKNOWN_CALLER = "+447123400999"
WHATSAPP_SENDER = "whatsapp:+14155238886"


def _log(session, garage, **overrides):
    fields = {
        "garage_id": garage.id,
        "channel": "VOICE",
        "direction": "INBOUND",
        "external_provider": "twilio",
        "status": "completed",
    }
    fields.update(overrides)
    row = CommunicationLog(**fields)
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


def test_overview_zero_state_with_no_data(authenticated_client):
    resp = authenticated_client.get("/api/communications/overview")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["calls_today"] == 0
    assert body["missed_calls_today"] == 0
    assert body["whatsapp_unread"] == 0
    assert body["outgoing_contacts_today"] == 0
    assert body["recent"] == []
    assert body["capabilities"]["communications_enabled"] is False
    assert body["capabilities"]["outbound_calling_supported"] is False


def test_overview_counts_reflect_real_data(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _log(session, garage, channel="VOICE", direction="INBOUND", status="completed", created_at=now)
    _log(session, garage, channel="VOICE", direction="INBOUND", status="no-answer", created_at=now)
    _log(
        session, garage, channel="WHATSAPP", direction="INBOUND", status="received",
        from_address=WHATSAPP_SENDER, to_address=WHATSAPP_SENDER, body="hi", read_at=None,
    )
    _log(
        session, garage, channel="WHATSAPP", direction="OUTBOUND", status="queued",
        from_address=WHATSAPP_SENDER, to_address="whatsapp:+447123400222", created_at=now,
    )

    resp = authenticated_client.get("/api/communications/overview")
    body = resp.get_json()
    assert body["calls_today"] == 2
    assert body["missed_calls_today"] == 1
    assert body["whatsapp_unread"] == 1
    assert body["outgoing_contacts_today"] == 1
    assert len(body["recent"]) == 4


def test_overview_requires_auth(client):
    assert client.get("/api/communications/overview").status_code == 401


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------


def test_calls_list_is_tenant_scoped(session, garage, second_garage, authenticated_client):
    _log(session, garage, from_address=CALLER)
    _log(session, second_garage, from_address=CALLER)

    resp = authenticated_client.get("/api/communications/calls")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["from_address"] == CALLER


def test_calls_list_known_customer_resolves(session, garage, customer, authenticated_client):
    _log(session, garage, from_address=customer.phone, customer_id=customer.id)

    resp = authenticated_client.get("/api/communications/calls")
    call = resp.get_json()["items"][0]
    assert call["customer"]["id"] == str(customer.id)
    assert call["customer"]["first_name"] == customer.first_name


def test_calls_list_unknown_caller_has_no_customer(session, garage, authenticated_client):
    _log(session, garage, from_address=UNKNOWN_CALLER)

    resp = authenticated_client.get("/api/communications/calls")
    call = resp.get_json()["items"][0]
    assert call["customer"] is None
    assert call["from_address"] == UNKNOWN_CALLER


def test_calls_list_filters_by_direction(session, garage, authenticated_client):
    _log(session, garage, direction="INBOUND", from_address=CALLER)
    _log(session, garage, direction="OUTBOUND", to_address=CALLER)

    resp = authenticated_client.get("/api/communications/calls?direction=OUTBOUND")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["direction"] == "OUTBOUND"


def test_calls_list_missed_only_filter(session, garage, authenticated_client):
    _log(session, garage, direction="INBOUND", status="completed")
    _log(session, garage, direction="INBOUND", status="no-answer")
    _log(session, garage, direction="OUTBOUND", status="no-answer")  # not "missed" - it's outbound

    resp = authenticated_client.get("/api/communications/calls?missed_only=true")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "no-answer"
    assert body["items"][0]["direction"] == "INBOUND"


def test_calls_list_search_by_customer_name(session, garage, customer, authenticated_client):
    _log(session, garage, from_address=customer.phone, customer_id=customer.id)
    _log(session, garage, from_address=UNKNOWN_CALLER)

    resp = authenticated_client.get(f"/api/communications/calls?search={customer.first_name}")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["customer"]["id"] == str(customer.id)


def test_calls_list_search_by_phone_number(session, garage, authenticated_client):
    _log(session, garage, from_address=CALLER)
    _log(session, garage, from_address=UNKNOWN_CALLER)

    resp = authenticated_client.get("/api/communications/calls?search=400111")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["from_address"] == CALLER


def test_calls_list_pagination(session, garage, authenticated_client):
    for _ in range(5):
        _log(session, garage, from_address=CALLER)

    resp = authenticated_client.get("/api/communications/calls?limit=2&offset=0")
    body = resp.get_json()
    assert body["total"] == 5
    assert len(body["items"]) == 2

    resp2 = authenticated_client.get("/api/communications/calls?limit=2&offset=4")
    assert len(resp2.get_json()["items"]) == 1


def test_call_detail_returns_own_tenant_call(session, garage, authenticated_client):
    call = _log(session, garage, from_address=CALLER)

    resp = authenticated_client.get(f"/api/communications/calls/{call.id}")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == str(call.id)


def test_call_detail_404s_for_other_tenants_call(session, second_garage, authenticated_client):
    other_call = _log(session, second_garage, from_address=CALLER)

    resp = authenticated_client.get(f"/api/communications/calls/{other_call.id}")
    assert resp.status_code == 404


def test_initiate_call_returns_not_yet_available(authenticated_client, customer):
    resp = authenticated_client.post(
        "/api/communications/calls", json={"customer_id": str(customer.id)}
    )
    assert resp.status_code == 501


def test_initiate_call_404s_for_other_tenants_customer(authenticated_client, second_customer):
    resp = authenticated_client.post(
        "/api/communications/calls", json={"customer_id": str(second_customer.id)}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# WhatsApp conversations
# --------------------------------------------------------------------------


def test_conversations_group_by_counterpart(session, garage, authenticated_client):
    addr_a = "whatsapp:+447123400001"
    addr_b = "whatsapp:+447123400002"
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr_a, to_address=WHATSAPP_SENDER, body="hi")
    _log(session, garage, channel="WHATSAPP", direction="OUTBOUND", from_address=WHATSAPP_SENDER, to_address=addr_a, body="hello back")
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr_b, to_address=WHATSAPP_SENDER, body="different person")

    resp = authenticated_client.get("/api/communications/conversations")
    body = resp.get_json()
    assert body["total"] == 2
    phones = {c["phone"] for c in body["items"]}
    assert phones == {"+447123400001", "+447123400002"}


def test_conversations_never_merge_across_tenants(session, garage, second_garage, authenticated_client):
    """The same real-world phone number messaging two different garages must
    show up as two completely separate conversations - see task requirement
    that a shared customer phone number across tenants never merges."""
    shared = "whatsapp:+447123400555"
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=shared, to_address=WHATSAPP_SENDER, body="to garage A")
    _log(session, second_garage, channel="WHATSAPP", direction="INBOUND", from_address=shared, to_address=WHATSAPP_SENDER, body="to garage B - should never appear")

    resp = authenticated_client.get("/api/communications/conversations")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["last_message"]["body"] == "to garage A"


def test_conversations_unread_count(session, garage, authenticated_client):
    addr = "whatsapp:+447123400003"
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="1", read_at=None)
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="2", read_at=None)
    _log(session, garage, channel="WHATSAPP", direction="OUTBOUND", from_address=WHATSAPP_SENDER, to_address=addr, body="reply")

    resp = authenticated_client.get("/api/communications/conversations")
    convo = resp.get_json()["items"][0]
    assert convo["unread_count"] == 2


def test_conversation_messages_are_chronological(session, garage, authenticated_client):
    addr = "whatsapp:+447123400004"
    now = datetime.now(UTC)
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="first", created_at=now - timedelta(minutes=10))
    _log(session, garage, channel="WHATSAPP", direction="OUTBOUND", from_address=WHATSAPP_SENDER, to_address=addr, body="second", created_at=now - timedelta(minutes=5))
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="third", created_at=now)

    resp = authenticated_client.get("/api/communications/conversations/+447123400004/messages")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["first", "second", "third"]


def test_conversation_messages_tenant_scoped_even_for_same_number(
    session, garage, second_garage, authenticated_client
):
    shared = "whatsapp:+447123400556"
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=shared, to_address=WHATSAPP_SENDER, body="garage A message")
    _log(session, second_garage, channel="WHATSAPP", direction="INBOUND", from_address=shared, to_address=WHATSAPP_SENDER, body="garage B message")

    resp = authenticated_client.get("/api/communications/conversations/+447123400556/messages")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["garage A message"]


def test_conversation_messages_unknown_number_returns_empty(garage, authenticated_client):
    resp = authenticated_client.get("/api/communications/conversations/+447123400777/messages")
    assert resp.status_code == 200
    assert resp.get_json()["messages"] == []


def test_conversation_messages_invalid_phone_returns_422(authenticated_client):
    resp = authenticated_client.get("/api/communications/conversations/not-a-number/messages")
    assert resp.status_code == 422


def test_mark_conversation_read_updates_only_that_threads_inbound_rows(
    session, garage, authenticated_client
):
    addr = "whatsapp:+447123400005"
    unread = _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="unread", read_at=None)
    other_thread_unread = _log(
        session, garage, channel="WHATSAPP", direction="INBOUND",
        from_address="whatsapp:+447123400006", to_address=WHATSAPP_SENDER, body="different thread", read_at=None,
    )

    resp = authenticated_client.post("/api/communications/conversations/+447123400005/read")
    assert resp.status_code == 200
    assert resp.get_json()["updated"] == 1

    session.refresh(unread)
    session.refresh(other_thread_unread)
    assert unread.read_at is not None
    assert other_thread_unread.read_at is None


def test_mark_conversation_read_is_idempotent(session, garage, authenticated_client):
    addr = "whatsapp:+447123400007"
    _log(session, garage, channel="WHATSAPP", direction="INBOUND", from_address=addr, to_address=WHATSAPP_SENDER, body="hi", read_at=None)

    first = authenticated_client.post("/api/communications/conversations/+447123400007/read")
    second = authenticated_client.post("/api/communications/conversations/+447123400007/read")

    assert first.get_json()["updated"] == 1
    assert second.get_json()["updated"] == 0


# --------------------------------------------------------------------------
# WhatsApp send
# --------------------------------------------------------------------------


def test_send_whatsapp_by_customer_id_is_skipped_without_twilio(session, garage, authenticated_client):
    from app.models.customer import Customer

    mobile_customer = Customer(
        garage_id=garage.id, first_name="Priya", last_name="Shah", phone="+447123400042"
    )
    session.add(mobile_customer)
    session.commit()

    resp = authenticated_client.post(
        "/api/communications/whatsapp/send",
        json={"customer_id": str(mobile_customer.id), "body": "Your MOT is booked for Friday."},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "SKIPPED_NOT_CONFIGURED"
    assert body["customer"]["id"] == str(mobile_customer.id)


def test_send_whatsapp_by_raw_number(authenticated_client):
    resp = authenticated_client.post(
        "/api/communications/whatsapp/send",
        json={"to": "07123 400321", "body": "Hi, following up on your enquiry."},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "SKIPPED_NOT_CONFIGURED"
    assert body["to_address"] == "whatsapp:+447123400321"


def test_send_whatsapp_requires_customer_id_or_to(authenticated_client):
    resp = authenticated_client.post("/api/communications/whatsapp/send", json={"body": "hi"})
    assert resp.status_code == 422


def test_send_whatsapp_404s_for_other_tenants_customer(authenticated_client, second_customer):
    resp = authenticated_client.post(
        "/api/communications/whatsapp/send",
        json={"customer_id": str(second_customer.id), "body": "hi"},
    )
    assert resp.status_code == 404


def test_send_whatsapp_422s_when_customer_has_no_phone(session, garage, authenticated_client):
    from app.models.customer import Customer

    no_phone_customer = Customer(garage_id=garage.id, first_name="No", last_name="Phone", phone=None)
    session.add(no_phone_customer)
    session.commit()

    resp = authenticated_client.post(
        "/api/communications/whatsapp/send",
        json={"customer_id": str(no_phone_customer.id), "body": "hi"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Customer-scoped communication history
# --------------------------------------------------------------------------


def test_customer_communications_only_shows_that_customers_rows(
    session, garage, customer, second_customer, authenticated_client
):
    mine = _log(session, garage, customer_id=customer.id, from_address=customer.phone)
    _log(session, garage, customer_id=None, from_address=UNKNOWN_CALLER)

    resp = authenticated_client.get(f"/api/customers/{customer.id}/communications")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.get_json()]
    assert ids == [str(mine.id)]


def test_customer_communications_404s_for_other_tenants_customer(authenticated_client, second_customer):
    resp = authenticated_client.get(f"/api/customers/{second_customer.id}/communications")
    assert resp.status_code == 404


def test_customer_communications_requires_auth(client, customer):
    resp = client.get(f"/api/customers/{customer.id}/communications")
    assert resp.status_code == 401
