"""WhatsApp/SMS/voice bookings for a business with Terms & Conditions: they
are held AWAITING_TERMS (reserving the slot) until the customer accepts the
terms on the texted page, then move on to PENDING - or AWAITING_PAYMENT for a
deposit booking. See app/terms/service.py.
"""

import datetime
import json
from unittest.mock import Mock

import pytest

from app.ai_voice import tools as tools_module
from app.ai_voice.instructions import build_instructions
from app.ai_voice.tools import dispatch_tool
from app.communications import events as comms_events
from app.communications import sms_automation
from app.conversation import actions
from app.email import service as email_service
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog
from app.models.payments.payment import BookingPayment
from app.payments.service import expire_stale_payment_holds
from app.terms.service import save_terms

TERMS = "Cancellations need 24 hours' notice."
SLOT = datetime.time(10, 0)


def _future_weekday(days_ahead=7):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


DAY = _future_weekday()


@pytest.fixture()
def with_terms(session, garage, garage_schedule, user):
    """Garage A with terms, the default schedule and one employee - so one
    booking fills a slot."""
    save_terms(garage, TERMS)
    return garage


def _book(garage, appointment_type, **overrides):
    kwargs = {
        "first_name": "Jo",
        "last_name": "Bloggs",
        "phone_e164": "+447123456789",
        "email": None,
        "vehicle_registration": "AB12CDE",
        "appointment_type": appointment_type,
        "preferred_date": DAY,
        "preferred_time": SLOT,
    }
    kwargs.update(overrides)
    return actions.create_booking_request(garage, **kwargs)


def _deposit_type(session, garage):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="Full Service",
        status="ACTIVE",
        default_duration_minutes=60,
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    session.add(t)
    session.commit()
    return t


def _lookup(client, garage, token):
    return client.post(f"/api/public/{garage.slug}/terms-acceptance/lookup", json={"token": token})


def _accept(client, garage, token, **overrides):
    body = {"token": token, "terms_accepted": True, "terms_version": garage.terms_version}
    body.update(overrides)
    return client.post(f"/api/public/{garage.slug}/terms-acceptance/accept", json=body)


@pytest.fixture()
def events(monkeypatch):
    """Every emitted event name, in order."""
    seen = []
    real = comms_events.emit_event

    def spy(name, **kwargs):
        seen.append(name)
        return real(name, **kwargs)

    monkeypatch.setattr(comms_events, "emit_event", spy)
    monkeypatch.setattr(actions, "emit_event", spy)
    return seen


# --------------------------------------------------------------------------
# Creation and the hold
# --------------------------------------------------------------------------


def test_without_terms_conversation_bookings_are_pending_as_before(
    session, garage, garage_schedule, appointment_type
):
    booking, reason = _book(garage, appointment_type)
    assert reason is None
    assert booking.status == "PENDING"
    assert booking.terms_acceptance_token_hash is None
    assert not hasattr(booking, "terms_acceptance_token")


def test_with_terms_a_conversation_booking_is_held_awaiting_terms(
    with_terms, appointment_type, events
):
    booking, _ = _book(with_terms, appointment_type)
    assert booking.status == "AWAITING_TERMS"
    assert booking.payment_hold_expires_at is not None
    assert booking.terms_accepted_at is None
    # Plaintext token for the link; only its hash is stored.
    assert len(booking.terms_acceptance_token) >= 32
    assert booking.terms_acceptance_token_hash != booking.terms_acceptance_token
    # Nothing tells the customer their request is received yet.
    assert comms_events.BOOKING_REQUEST_CREATED not in events


def test_the_hold_reserves_the_slot(with_terms, appointment_type):
    _book(with_terms, appointment_type)
    second, reason = _book(with_terms, appointment_type, phone_e164="+447700900111")
    assert second is None
    assert reason == "full"


def test_an_ignored_hold_expires_and_releases_the_slot(session, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    later = booking.payment_hold_expires_at + datetime.timedelta(seconds=1)
    assert expire_stale_payment_holds(garage_id=with_terms.id, now=later) == 1
    session.expire_all()
    assert BookingRequest.query.get(booking.id).status == "EXPIRED"

    again, reason = _book(with_terms, appointment_type, phone_e164="+447700900111")
    assert reason is None
    assert again.status == "AWAITING_TERMS"


def test_staff_cannot_approve_until_terms_are_accepted(
    authenticated_client, with_terms, appointment_type, user
):
    booking, _ = _book(with_terms, appointment_type)
    resp = authenticated_client.post(
        f"/api/booking-requests/{booking.id}/approve",
        json={"employee_id": str(user.id), "appointment_type_id": str(appointment_type.id)},
    )
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "awaiting_terms"
    listed = authenticated_client.get("/api/booking-requests/?status=AWAITING_TERMS").get_json()
    assert [row["id"] for row in listed] == [str(booking.id)]
    assert listed[0]["hold_expires_at"] is not None


def test_no_received_or_confirmed_message_goes_out_while_awaiting_terms(
    app, with_terms, appointment_type, monkeypatch
):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms = Mock()
    monkeypatch.setattr(sms_automation, "send_sms_message", sms)
    booking, _ = _book(with_terms, appointment_type)
    # Even if something did emit the event, the handlers refuse it.
    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_CREATED, garage=with_terms, booking_request=booking
    )
    sms.assert_not_called()
    assert CommunicationLog.query.filter_by(booking_request_id=booking.id).count() == 0


# --------------------------------------------------------------------------
# The acceptance page
# --------------------------------------------------------------------------


def test_lookup_shows_the_booking_and_the_current_terms(client, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    body = _lookup(client, with_terms, booking.terms_acceptance_token).get_json()
    assert body["state"] == "AWAITING_ACCEPTANCE"
    assert body["terms_and_conditions"] == TERMS
    assert body["terms_version"] == 1
    assert body["booking"]["booking_reference"] == booking.booking_reference
    assert body["booking"]["preferred_date"] == DAY.isoformat()
    assert body["booking"]["deposit_required"] is False


def test_accepting_moves_the_request_to_pending_and_records_it(
    client, with_terms, appointment_type, events
):
    booking, _ = _book(with_terms, appointment_type)
    resp = _accept(client, with_terms, booking.terms_acceptance_token)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "PENDING"
    assert resp.get_json()["recovery_token"] is None

    stored = BookingRequest.query.get(booking.id)
    assert stored.status == "PENDING"
    assert stored.terms_version == 1
    assert stored.terms_accepted_at is not None
    assert stored.payment_hold_expires_at is None
    assert stored.source == "CONVERSATION"
    assert comms_events.BOOKING_REQUEST_CREATED in events


def test_customer_sees_and_accepts_the_new_wording_if_it_changed_since_booking(
    client, with_terms, appointment_type
):
    booking, _ = _book(with_terms, appointment_type)
    save_terms(with_terms, "Wording changed after the call")
    body = _lookup(client, with_terms, booking.terms_acceptance_token).get_json()
    assert body["terms_and_conditions"] == "Wording changed after the call"
    assert body["terms_version"] == 2
    assert _accept(client, with_terms, booking.terms_acceptance_token).status_code == 200
    assert BookingRequest.query.get(booking.id).terms_version == 2


def test_an_edit_while_the_page_is_open_is_caught(client, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    # The page loaded version 1; the owner saves version 2 before they tick.
    save_terms(with_terms, "Edited mid-read")
    resp = _accept(client, with_terms, booking.terms_acceptance_token, terms_version=1)
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "terms_version_mismatch"
    assert BookingRequest.query.get(booking.id).status == "AWAITING_TERMS"


def test_acceptance_requires_the_box(client, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    resp = _accept(client, with_terms, booking.terms_acceptance_token, terms_accepted=False)
    assert resp.status_code == 422
    assert BookingRequest.query.get(booking.id).status == "AWAITING_TERMS"


def test_a_used_token_cannot_be_reused(client, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    token = booking.terms_acceptance_token
    _accept(client, with_terms, token)
    again = _accept(client, with_terms, token)
    assert again.status_code == 409
    assert again.get_json()["errors"]["reason"] == "terms_already_accepted"
    lookup = _lookup(client, with_terms, token).get_json()
    assert lookup["state"] == "ACCEPTED"
    assert lookup["terms_and_conditions"] is None


def test_invalid_and_cross_business_tokens_look_the_same(
    client, session, with_terms, appointment_type, second_garage
):
    booking, _ = _book(with_terms, appointment_type)
    assert _lookup(client, with_terms, "x" * 43).status_code == 404
    # A real token for A, presented to B.
    save_terms(second_garage, "B's terms")
    assert _lookup(client, second_garage, booking.terms_acceptance_token).status_code == 404
    assert _accept(client, second_garage, booking.terms_acceptance_token).status_code == 404
    assert BookingRequest.query.get(booking.id).status == "AWAITING_TERMS"


def test_an_expired_hold_cannot_be_accepted(client, session, with_terms, appointment_type):
    booking, _ = _book(with_terms, appointment_type)
    booking.payment_hold_expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )
    session.commit()
    assert _lookup(client, with_terms, booking.terms_acceptance_token).get_json()["state"] == (
        "EXPIRED"
    )
    resp = _accept(client, with_terms, booking.terms_acceptance_token)
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "terms_acceptance_expired"
    assert BookingRequest.query.get(booking.id).status == "EXPIRED"


# --------------------------------------------------------------------------
# Deposit bookings: terms first, then payment
# --------------------------------------------------------------------------


def test_deposit_booking_takes_no_payment_until_terms_are_accepted(client, session, with_terms):
    deposit_type = _deposit_type(session, with_terms)
    booking, _ = actions.create_deposit_booking_request(
        with_terms,
        first_name="Jo",
        last_name="Bloggs",
        phone_e164="+447123456789",
        email=None,
        vehicle_registration="AB12CDE",
        appointment_type=deposit_type,
        preferred_date=DAY,
        preferred_time=SLOT,
    )
    assert booking.status == "AWAITING_TERMS"
    assert BookingPayment.query.count() == 0
    assert booking.payment_recovery_token_hash is None
    assert not hasattr(booking, "deposit_recovery_token")

    lookup = _lookup(client, with_terms, booking.terms_acceptance_token).get_json()
    assert lookup["booking"]["deposit_required"] is True

    resp = _accept(client, with_terms, booking.terms_acceptance_token)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "AWAITING_PAYMENT"
    assert body["recovery_token"]

    stored = BookingRequest.query.get(booking.id)
    assert stored.terms_version == 1
    assert stored.payment_hold_expires_at is not None
    assert BookingPayment.query.filter_by(booking_request_id=booking.id).count() == 1

    # The returned capability drives the existing checkout recovery.
    recovered = client.post(
        f"/api/public/{with_terms.slug}/booking-requests/deposit-attempt/recover",
        json={"recovery_token": body["recovery_token"]},
    )
    assert recovered.status_code == 200
    assert recovered.get_json()["status"] == "AWAITING_PAYMENT"
    assert recovered.get_json()["provider_data"]


# --------------------------------------------------------------------------
# Voice tool, text workflow, prompts
# --------------------------------------------------------------------------


def test_voice_booking_texts_the_acceptance_link_and_reports_the_hold(
    monkeypatch, with_terms, appointment_type
):
    sent = {}

    def fake_send(*, garage, to, body, booking_request=None, **_kw):
        sent.update(to=to, body=body)
        return CommunicationLog(
            garage_id=garage.id, channel="SMS", direction="OUTBOUND", status="queued"
        )

    monkeypatch.setattr(tools_module, "send_sms_message", fake_send)
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": DAY.isoformat(),
            "time": "10:00",
            "first_name": "Jo",
            "last_name": "Bloggs",
            "vehicle_registration": "AB12 CDE",
        }
    )
    result = json.loads(dispatch_tool(with_terms, "+447123456789", "create_booking", args))
    assert result["ok"] is True
    assert result["status"] == "AWAITING_TERMS"
    assert result["terms_acceptance_required"] is True
    assert result["terms_link_sent_by_sms"] is True
    assert sent["to"] == "+447123456789"
    assert f"/accept-terms/{with_terms.id}#" in sent["body"]
    assert "isn't confirmed" in sent["body"]


def test_voice_deposit_booking_sends_one_link_for_terms_then_payment(
    monkeypatch, session, with_terms
):
    deposit_type = _deposit_type(session, with_terms)
    bodies = []

    def fake_send(*, garage, to, body, booking_request=None, **_kw):
        bodies.append(body)
        return CommunicationLog(
            garage_id=garage.id, channel="SMS", direction="OUTBOUND", status="queued"
        )

    monkeypatch.setattr(tools_module, "send_sms_message", fake_send)
    args = json.dumps(
        {
            "appointment_type_id": str(deposit_type.id),
            "date": DAY.isoformat(),
            "time": "10:00",
            "first_name": "Jo",
            "last_name": "Bloggs",
        }
    )
    result = json.loads(dispatch_tool(with_terms, "+447123456789", "create_booking", args))
    assert result["status"] == "AWAITING_TERMS"
    assert result["deposit_required"] is True
    assert result["terms_link_sent_by_sms"] is True
    assert "payment_link_sent_by_sms" not in result
    assert len(bodies) == 1 and "/accept-terms/" in bodies[0]
    assert BookingPayment.query.count() == 0


def test_voice_prompt_mentions_terms_only_when_the_business_has_them(garage):
    assert "terms and conditions" not in build_instructions(garage)
    save_terms(garage, TERMS)
    prompt = build_instructions(garage)
    assert "terms_acceptance_required" in prompt
    assert "never that it's booked" in prompt


def test_text_conversation_sends_the_link_and_does_not_claim_a_booking(
    session, with_terms, appointment_type
):
    from tests.test_conversation_engine import _next_open_weekday, _now, _send

    now = _now()
    day = _next_open_weekday(now)
    phone = "+447123400099"
    _send(with_terms, phone, "I need an MOT", now=now)
    _send(with_terms, phone, "Jane Doe", now=now)
    r3 = _send(with_terms, phone, day.strftime("%A"), now=now)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    _send(with_terms, phone, first_slot, now=now)
    summary = _send(with_terms, phone, "AB12 CDE", now=now)
    assert "terms and conditions" in summary.response_text

    done = _send(with_terms, phone, "yes", now=now)
    assert f"/accept-terms/{with_terms.id}#" in done.response_text
    assert "isn't booked" in done.response_text
    assert "all set" not in done.response_text
    assert BookingRequest.query.one().status == "AWAITING_TERMS"


# --------------------------------------------------------------------------
# Confirmations link the accepted version
# --------------------------------------------------------------------------


def test_received_messages_link_the_exact_accepted_version(
    app, client, monkeypatch, with_terms, appointment_type
):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms = Mock()
    monkeypatch.setattr(sms_automation, "send_sms_message", sms)
    emails = []
    monkeypatch.setattr(email_service, "send_email", lambda **kw: emails.append(kw))
    sms_automation.register_sms_handlers()

    booking, _ = _book(with_terms, appointment_type, email="jo@example.com")
    _accept(client, with_terms, booking.terms_acceptance_token)
    save_terms(with_terms, "Later wording")  # must not change the link

    link = f"/terms/{with_terms.id}/v/1"
    assert any(link in call.kwargs["body"] for call in sms.call_args_list)
    assert "Terms you accepted:" in sms.call_args_list[-1].kwargs["body"]
    assert emails and any(link in str(e) for e in emails)


def test_messages_have_no_terms_line_without_acceptance(
    app, monkeypatch, session, garage, garage_schedule, appointment_type
):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms = Mock()
    monkeypatch.setattr(sms_automation, "send_sms_message", sms)
    sms_automation.register_sms_handlers()
    _book(garage, appointment_type)
    assert sms.called
    assert "Terms" not in sms.call_args.kwargs["body"]


def test_approval_confirmation_links_the_accepted_version(
    app, client, authenticated_client, monkeypatch, with_terms, appointment_type, user
):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms = Mock()
    monkeypatch.setattr(sms_automation, "send_sms_message", sms)
    sms_automation.register_sms_handlers()

    booking, _ = _book(with_terms, appointment_type)
    _accept(client, with_terms, booking.terms_acceptance_token)
    resp = authenticated_client.post(
        f"/api/booking-requests/{booking.id}/approve",
        json={"employee_id": str(user.id), "appointment_type_id": str(appointment_type.id)},
    )
    assert resp.status_code == 200
    confirmed = [c.kwargs["body"] for c in sms.call_args_list if "confirmed" in c.kwargs["body"]]
    assert confirmed and f"/terms/{with_terms.id}/v/1" in confirmed[-1]
    # Acceptance stays on the request after approval.
    assert resp.get_json()["terms_version"] == 1
