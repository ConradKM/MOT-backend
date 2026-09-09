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
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        status="received",
        from_address=WHATSAPP_SENDER,
        to_address=WHATSAPP_SENDER,
        body="hi",
        read_at=None,
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="OUTBOUND",
        status="queued",
        from_address=WHATSAPP_SENDER,
        to_address="whatsapp:+447123400222",
        created_at=now,
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
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr_a,
        to_address=WHATSAPP_SENDER,
        body="hi",
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="OUTBOUND",
        from_address=WHATSAPP_SENDER,
        to_address=addr_a,
        body="hello back",
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr_b,
        to_address=WHATSAPP_SENDER,
        body="different person",
    )

    resp = authenticated_client.get("/api/communications/conversations")
    body = resp.get_json()
    assert body["total"] == 2
    phones = {c["phone"] for c in body["items"]}
    assert phones == {"+447123400001", "+447123400002"}


def test_conversations_never_merge_across_tenants(
    session, garage, second_garage, authenticated_client
):
    """The same real-world phone number messaging two different garages must
    show up as two completely separate conversations - see task requirement
    that a shared customer phone number across tenants never merges."""
    shared = "whatsapp:+447123400555"
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=shared,
        to_address=WHATSAPP_SENDER,
        body="to garage A",
    )
    _log(
        session,
        second_garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=shared,
        to_address=WHATSAPP_SENDER,
        body="to garage B - should never appear",
    )

    resp = authenticated_client.get("/api/communications/conversations")
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["last_message"]["body"] == "to garage A"


def test_conversations_unread_count(session, garage, authenticated_client):
    addr = "whatsapp:+447123400003"
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="1",
        read_at=None,
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="2",
        read_at=None,
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="OUTBOUND",
        from_address=WHATSAPP_SENDER,
        to_address=addr,
        body="reply",
    )

    resp = authenticated_client.get("/api/communications/conversations")
    convo = resp.get_json()["items"][0]
    assert convo["unread_count"] == 2


def test_conversation_messages_are_chronological(session, garage, authenticated_client):
    addr = "whatsapp:+447123400004"
    now = datetime.now(UTC)
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="first",
        created_at=now - timedelta(minutes=10),
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="OUTBOUND",
        from_address=WHATSAPP_SENDER,
        to_address=addr,
        body="second",
        created_at=now - timedelta(minutes=5),
    )
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="third",
        created_at=now,
    )

    resp = authenticated_client.get("/api/communications/conversations/+447123400004/messages")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["first", "second", "third"]


def test_conversation_messages_tenant_scoped_even_for_same_number(
    session, garage, second_garage, authenticated_client
):
    shared = "whatsapp:+447123400556"
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=shared,
        to_address=WHATSAPP_SENDER,
        body="garage A message",
    )
    _log(
        session,
        second_garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=shared,
        to_address=WHATSAPP_SENDER,
        body="garage B message",
    )

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
    unread = _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="unread",
        read_at=None,
    )
    other_thread_unread = _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address="whatsapp:+447123400006",
        to_address=WHATSAPP_SENDER,
        body="different thread",
        read_at=None,
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
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=addr,
        to_address=WHATSAPP_SENDER,
        body="hi",
        read_at=None,
    )

    first = authenticated_client.post("/api/communications/conversations/+447123400007/read")
    second = authenticated_client.post("/api/communications/conversations/+447123400007/read")

    assert first.get_json()["updated"] == 1
    assert second.get_json()["updated"] == 0


# --------------------------------------------------------------------------
# WhatsApp send
# --------------------------------------------------------------------------


def test_send_whatsapp_by_customer_id_is_skipped_without_twilio(
    session, garage, authenticated_client
):
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

    no_phone_customer = Customer(
        garage_id=garage.id, first_name="No", last_name="Phone", phone=None
    )
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


def test_customer_communications_404s_for_other_tenants_customer(
    authenticated_client, second_customer
):
    resp = authenticated_client.get(f"/api/customers/{second_customer.id}/communications")
    assert resp.status_code == 404


def test_customer_communications_requires_auth(client, customer):
    resp = client.get(f"/api/customers/{customer.id}/communications")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Voice call vs. ConversationRelay transcript turns (issue #63)
#
# Every automated call writes one call-level VOICE row (external_provider
# "twilio") plus one CommunicationLog row per transcript turn (provider
# "comaz_conversation_engine"). Turns must group under the call and never be
# counted or listed as calls of their own.
# --------------------------------------------------------------------------

ENGINE = "comaz_conversation_engine"


def _call_row(session, garage, call_sid, **overrides):
    return _log(
        session,
        garage,
        external_provider="twilio",
        external_id=call_sid,
        call_sid=call_sid,
        from_address=CALLER,
        **overrides,
    )


def _turn_rows(session, garage, call_sid, n, *, base_time=None):
    base_time = base_time or datetime.now(UTC)
    rows = []
    for i in range(n):
        direction = "INBOUND" if i % 2 == 0 else "OUTBOUND"
        rows.append(
            _log(
                session,
                garage,
                external_provider=ENGINE,
                external_id=f"{call_sid}:{i + 1}" if direction == "INBOUND" else None,
                call_sid=call_sid,
                direction=direction,
                status="received" if direction == "INBOUND" else "sent",
                body=f"turn {i + 1}",
                created_at=base_time + timedelta(seconds=i),
            )
        )
    return rows


def test_one_call_with_ten_turns_counts_as_one_call(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _call_row(session, garage, "CA111", created_at=now, status="completed")
    _turn_rows(session, garage, "CA111", 10, base_time=now)

    body = authenticated_client.get("/api/communications/overview").get_json()
    assert body["calls_today"] == 1
    assert len(body["recent"]) == 1
    assert body["recent"][0]["external_provider"] == "twilio"

    calls = authenticated_client.get("/api/communications/calls").get_json()
    assert calls["total"] == 1
    assert len(calls["items"]) == 1


def test_two_calls_with_many_turns_count_as_two(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _call_row(session, garage, "CA-A", created_at=now, status="completed")
    _turn_rows(session, garage, "CA-A", 6, base_time=now)
    _call_row(session, garage, "CA-B", created_at=now, status="completed")
    _turn_rows(session, garage, "CA-B", 4, base_time=now)

    body = authenticated_client.get("/api/communications/overview").get_json()
    assert body["calls_today"] == 2

    calls = authenticated_client.get("/api/communications/calls").get_json()
    assert calls["total"] == 2


def test_missed_call_count_is_distinct_calls_not_turns(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _call_row(session, garage, "CA-M1", created_at=now, direction="INBOUND", status="no-answer")
    _call_row(session, garage, "CA-M2", created_at=now, direction="INBOUND", status="busy")
    _turn_rows(session, garage, "CA-M1", 8, base_time=now)

    body = authenticated_client.get("/api/communications/overview").get_json()
    assert body["missed_calls_today"] == 2


def test_call_detail_includes_the_transcript(session, garage, authenticated_client):
    now = datetime.now(UTC)
    call = _call_row(session, garage, "CA-DET", created_at=now, status="completed")
    _log(
        session,
        garage,
        external_provider=ENGINE,
        call_sid="CA-DET",
        direction="INBOUND",
        external_id="CA-DET:1",
        status="received",
        body="hello",
        created_at=now,
    )
    _log(
        session,
        garage,
        external_provider=ENGINE,
        call_sid="CA-DET",
        direction="OUTBOUND",
        status="sent",
        body="Hi, how can I help?",
        created_at=now + timedelta(seconds=1),
    )
    _log(
        session,
        garage,
        external_provider=ENGINE,
        call_sid="CA-DET",
        direction="SYSTEM",
        status="sent",
        body="Booking request #abcd1234 created",
        created_at=now + timedelta(seconds=2),
    )

    resp = authenticated_client.get(f"/api/communications/calls/{call.id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == str(call.id)
    assert [t["body"] for t in body["transcript"]] == [
        "hello",
        "Hi, how can I help?",
        "Booking request #abcd1234 created",
    ]


def test_transcript_turn_id_is_not_itself_a_call(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _call_row(session, garage, "CA-X", created_at=now, status="completed")
    turns = _turn_rows(session, garage, "CA-X", 4, base_time=now)

    # A transcript-turn row id is not a call - the detail endpoint 404s it.
    resp = authenticated_client.get(f"/api/communications/calls/{turns[0].id}")
    assert resp.status_code == 404


def test_call_grouping_never_crosses_tenants(session, garage, second_garage, authenticated_client):
    now = datetime.now(UTC)
    call = _call_row(session, garage, "CA-DUP", created_at=now, status="completed")
    # Same CallSid string, different tenant - must not be pulled in.
    _log(
        session,
        second_garage,
        external_provider=ENGINE,
        call_sid="CA-DUP",
        direction="INBOUND",
        status="received",
        body="other tenant turn",
        created_at=now,
    )

    body = authenticated_client.get(f"/api/communications/calls/{call.id}").get_json()
    assert body["transcript"] == []


def test_whatsapp_overview_is_unchanged_by_call_grouping(session, garage, authenticated_client):
    now = datetime.now(UTC)
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        status="received",
        from_address=WHATSAPP_SENDER,
        to_address=WHATSAPP_SENDER,
        body="hi",
        created_at=now,
    )
    _call_row(session, garage, "CA-WA", created_at=now, status="completed")
    _turn_rows(session, garage, "CA-WA", 5, base_time=now)

    body = authenticated_client.get("/api/communications/overview").get_json()
    assert body["whatsapp_unread"] == 1
    assert body["calls_today"] == 1
    # recent = 1 WhatsApp message + 1 call (not 1 + 6)
    assert len(body["recent"]) == 2


# --------------------------------------------------------------------------
# Conversation management: archive / restore / delete + filters (issue #79)
# --------------------------------------------------------------------------

ADDR = "whatsapp:+447123400777"
PHONE = "+447123400777"


def _wa(session, garage, direction="INBOUND", **over):
    return _log(
        session,
        garage,
        channel="WHATSAPP",
        direction=direction,
        from_address=ADDR if direction == "INBOUND" else WHATSAPP_SENDER,
        to_address=WHATSAPP_SENDER if direction == "INBOUND" else ADDR,
        body="hi",
        **over,
    )


def _phones(resp):
    return {c["phone"] for c in resp.get_json()["items"]}


def test_archive_removes_a_thread_from_the_inbox_and_restore_brings_it_back(
    session, garage, authenticated_client
):
    _wa(session, garage)
    assert PHONE in _phones(authenticated_client.get("/api/communications/conversations"))

    assert (
        authenticated_client.post(f"/api/communications/conversations/{PHONE}/archive").status_code
        == 204
    )
    assert PHONE not in _phones(authenticated_client.get("/api/communications/conversations"))
    assert PHONE in _phones(
        authenticated_client.get("/api/communications/conversations?filter=archived")
    )
    item = authenticated_client.get("/api/communications/conversations?filter=archived").get_json()[
        "items"
    ][0]
    assert item["archived"] is True

    authenticated_client.post(f"/api/communications/conversations/{PHONE}/restore")
    assert PHONE in _phones(authenticated_client.get("/api/communications/conversations"))


def test_needs_attention_filter_shows_unread_threads_only(session, garage, authenticated_client):
    _wa(session, garage, read_at=None)  # unread
    other = "whatsapp:+447123400888"
    _log(
        session,
        garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=other,
        to_address=WHATSAPP_SENDER,
        body="read one",
        read_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    phones = _phones(
        authenticated_client.get("/api/communications/conversations?filter=needs_attention")
    )
    assert phones == {PHONE}


def test_archived_thread_is_excluded_from_needs_attention(session, garage, authenticated_client):
    _wa(session, garage, read_at=None)
    authenticated_client.post(f"/api/communications/conversations/{PHONE}/archive")
    phones = _phones(
        authenticated_client.get("/api/communications/conversations?filter=needs_attention")
    )
    assert PHONE not in phones


def test_archive_state_is_tenant_scoped(
    session, garage, second_garage, second_user, authenticated_client
):
    _wa(session, garage)
    _log(
        session,
        second_garage,
        channel="WHATSAPP",
        direction="INBOUND",
        from_address=ADDR,
        to_address=WHATSAPP_SENDER,
        body="garage B",
    )
    authenticated_client.post(f"/api/communications/conversations/{PHONE}/archive")

    # Garage A: archived. Garage B: still in its own inbox.
    assert PHONE not in _phones(authenticated_client.get("/api/communications/conversations"))
    from app.communications import queries

    b_items, _ = queries.list_conversations(second_garage)
    assert any(c["phone"] == PHONE and c["archived"] is False for c in b_items)


def test_soft_delete_is_owner_only(session, garage, staff_role, client):
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.employee import Employee
    from tests.conftest import DEFAULT_PASSWORD

    _wa(session, garage)
    staff = Employee(
        garage_id=garage.id,
        email="staff-a@garage-a.example",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    token = create_access_token(identity=str(staff.id))

    resp = client.delete(
        f"/api/communications/conversations/{PHONE}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_soft_delete_hides_from_every_filter_but_keeps_history(
    session, garage, authenticated_client
):
    _wa(session, garage)
    assert (
        authenticated_client.delete(f"/api/communications/conversations/{PHONE}").status_code == 204
    )
    for f in ("inbox", "archived", "needs_attention"):
        assert PHONE not in _phones(
            authenticated_client.get(f"/api/communications/conversations?filter={f}")
        )
    msgs = authenticated_client.get(
        f"/api/communications/conversations/{PHONE}/messages"
    ).get_json()
    assert len(msgs["messages"]) == 1  # nothing unsent, history intact
