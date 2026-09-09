"""Tests for the conversation/booking engine (app/conversation).

Runs real messages through engine.handle_message() directly - no Twilio, no
HTTP - exactly like the development simulator does. `now` is always pinned
explicitly so tests never depend on which real-world weekday they happen to
run on.
"""

import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.conversation import automation, engine, session_service
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog
from app.models.conversation.callback_request import CallbackRequest
from app.models.conversation.conversation_session import ConversationSession
from app.models.customer import Customer
from app.models.vehicle import Vehicle

PHONE_RAW = "+447123400010"


@pytest.fixture(autouse=True)
def _register_automation_handlers():
    # Defensive re-registration (idempotent) - a test in test_communications.py
    # wipes the shared handler registry; this guarantees the real automation
    # handlers are present regardless of test execution order.
    automation.register_default_handlers()
    yield


def _next_open_weekday(start: datetime, min_days_ahead: int = 3):
    d = (start + timedelta(days=min_days_ahead)).date()
    while d.weekday() >= 5:  # Sat/Sun - the default schedule is closed
        d += timedelta(days=1)
    return d


def _now():
    # A fixed Monday 08:00 - safely inside every test's lead-time window
    # regardless of which weekday actually runs the suite.
    base = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)  # a Monday
    assert base.weekday() == 0
    return base


def _send(garage, phone, text, *, channel="WHATSAPP", now=None, external_message_id=None):
    return engine.handle_message(
        garage,
        channel=channel,
        phone_e164=phone,
        text=text,
        now=now or _now(),
        external_message_id=external_message_id,
    )


# --------------------------------------------------------------------------
# Full booking flow (new, unrecognised customer)
# --------------------------------------------------------------------------


def test_full_booking_flow_creates_pending_request(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    target_day = _next_open_weekday(now)
    weekday_name = target_day.strftime("%A")

    r1 = _send(garage, PHONE_RAW, "I need an MOT", now=now)
    assert r1.intent == "CREATE_BOOKING"
    assert r1.workflow_step == "AWAITING_NAME"  # unrecognised number

    r2 = _send(garage, PHONE_RAW, "Jane Doe", now=now)
    assert r2.workflow_step == "AWAITING_DATE"

    r3 = _send(garage, PHONE_RAW, weekday_name, now=now)
    assert r3.workflow_step == "AWAITING_TIME"
    # Sanity: a real "09:00, 09:30, ..." slot list, not a template placeholder.
    assert re.search(r"\d{1,2}:\d{2}", r3.response_text)

    # Pull the first offered time out of the response ("... I have 09:00, ...")
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]

    r4 = _send(garage, PHONE_RAW, first_slot, now=now)
    assert r4.workflow_step == "AWAITING_VEHICLE_REG"

    r5 = _send(garage, PHONE_RAW, "AB12 CDE", now=now)
    assert r5.workflow_step == "AWAITING_BOOKING_CONFIRMATION"
    assert "AB12CDE" in r5.response_text
    assert appointment_type.name in r5.response_text

    r6 = _send(garage, PHONE_RAW, "yes", now=now)
    assert r6.workflow_step is None
    assert len(r6.actions_performed) == 1
    assert "Booking request" in r6.actions_performed[0]

    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.status == "PENDING"
    assert booking_request.customer_first_name == "Jane"
    assert booking_request.customer_last_name == "Doe"
    assert booking_request.customer_phone == PHONE_RAW
    assert booking_request.customer_email is None
    assert booking_request.vehicle_registration == "AB12CDE"
    assert booking_request.appointment_type_id == appointment_type.id
    assert booking_request.preferred_date == target_day


def test_booking_conversation_is_logged_for_staff_visibility(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)

    logs = (
        CommunicationLog.query.filter_by(garage_id=garage.id)
        .order_by(CommunicationLog.created_at)
        .all()
    )
    assert len(logs) == 2  # customer turn + bot reply
    assert logs[0].direction == "INBOUND"
    assert logs[0].body == "I need an MOT"
    assert logs[0].intent == "CREATE_BOOKING"
    assert logs[1].direction == "OUTBOUND"
    assert logs[1].body


# --------------------------------------------------------------------------
# Existing customer identification + multiple vehicles
# --------------------------------------------------------------------------


def test_known_customer_is_identified_by_phone(
    session, garage, garage_schedule, appointment_type, user
):
    known = Customer(
        garage_id=garage.id, first_name="Oliver", last_name="Bennett", phone="+447123400020"
    )
    session.add(known)
    session.commit()

    now = _now()
    r1 = _send(garage, "+447123400020", "I need an MOT", now=now)
    assert r1.workflow_step == "AWAITING_DATE"  # name step skipped - already known

    conv_session = ConversationSession.query.filter_by(garage_id=garage.id).one()
    assert conv_session.customer_id == known.id


def test_multiple_vehicles_asks_which_one(session, garage, garage_schedule, appointment_type, user):
    known = Customer(
        garage_id=garage.id, first_name="Oliver", last_name="Bennett", phone="+447123400021"
    )
    session.add(known)
    session.commit()
    v1 = Vehicle(
        garage_id=garage.id,
        customer_id=known.id,
        registration_number="AB12CDE",
        make="Audi",
        model="A4",
    )
    v2 = Vehicle(
        garage_id=garage.id,
        customer_id=known.id,
        registration_number="XY19ABC",
        make="BMW",
        model="320d",
    )
    session.add_all([v1, v2])
    session.commit()

    now = _now()
    target_day = _next_open_weekday(now)
    weekday_name = target_day.strftime("%A")

    _send(garage, "+447123400021", "I need an MOT", now=now)
    r2 = _send(garage, "+447123400021", weekday_name, now=now)
    first_slot = r2.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r3 = _send(garage, "+447123400021", first_slot, now=now)

    assert r3.workflow_step == "AWAITING_VEHICLE_CHOICE"
    assert "AB12CDE" in r3.response_text
    assert "XY19ABC" in r3.response_text

    r4 = _send(garage, "+447123400021", "2", now=now)
    assert r4.workflow_step == "AWAITING_BOOKING_CONFIRMATION"
    assert "XY19ABC" in r4.response_text


def test_single_known_vehicle_is_confirmed_not_silently_assumed(
    session, garage, garage_schedule, appointment_type, user
):
    known = Customer(
        garage_id=garage.id, first_name="Oliver", last_name="Bennett", phone="+447123400022"
    )
    session.add(known)
    session.commit()
    v1 = Vehicle(
        garage_id=garage.id,
        customer_id=known.id,
        registration_number="AB12CDE",
        make="Audi",
        model="A4",
    )
    session.add(v1)
    session.commit()

    now = _now()
    target_day = _next_open_weekday(now)
    weekday_name = target_day.strftime("%A")

    _send(garage, "+447123400022", "I need an MOT", now=now)
    r2 = _send(garage, "+447123400022", weekday_name, now=now)
    first_slot = r2.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r3 = _send(garage, "+447123400022", first_slot, now=now)

    assert r3.workflow_step == "AWAITING_VEHICLE_CONFIRM"
    assert "AB12CDE" in r3.response_text

    r4 = _send(garage, "+447123400022", "yes", now=now)
    assert r4.workflow_step == "AWAITING_BOOKING_CONFIRMATION"

    _send(garage, "+447123400022", "yes", now=now)
    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.vehicle_registration == "AB12CDE"
    assert booking_request.customer_id == known.id


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_offers_alternatives_when_requested_day_is_full(
    session, garage, garage_schedule, appointment_type, user, customer
):
    now = _now()
    target_day = _next_open_weekday(now)
    weekday_name = target_day.strftime("%A")

    # Fill every slot that day - default schedule is Mon-Fri 09:00-17:00,
    # 30-min slots, capacity = active employee count (1, from `user`).
    # `customer` here is just a real row to satisfy the FK - a different
    # phone number to PHONE_RAW, so it never collides with the conversation.
    from app.public_booking.availability import day_slots, resolve_opening_hours, resolve_settings

    settings = resolve_settings(garage)
    hours_map = resolve_opening_hours(garage)
    slots = day_slots(
        garage,
        target_day,
        settings,
        hours_map,
        {},
        now,
        duration_min=appointment_type.default_duration_minutes,
    )
    for i, s in enumerate(slots):
        hh, mm = (int(part) for part in s["start"].split(":"))
        start = datetime.combine(target_day, time(hh, mm), tzinfo=UTC)
        appt = Appointment(
            garage_id=garage.id,
            employee_id=user.id,
            customer_id=customer.id,
            appointment_type_id=appointment_type.id,
            start_time=start,
            end_time=start + timedelta(minutes=appointment_type.default_duration_minutes),
            status="BOOKED",
        )
        session.add(appt)
    session.commit()

    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r = _send(garage, PHONE_RAW, weekday_name, now=now)

    assert r.workflow_step == "AWAITING_DATE"
    assert "next available" in r.response_text.lower()


def test_price_query_reads_real_appointment_type_price(
    session, garage, garage_schedule, appointment_type, user
):
    # The shared `appointment_type` fixture leaves base_price unset - give it
    # a real one here, since this test is specifically about echoing back
    # whatever the business actually configured.
    appointment_type.base_price = Decimal("54.99")
    session.commit()

    r = _send(garage, PHONE_RAW, "how much is an MOT", now=_now())
    assert r.intent == "APPOINTMENT_PRICE_QUERY"
    assert f"£{appointment_type.base_price}" in r.response_text


def test_price_query_without_a_configured_price_offers_a_callback_instead_of_inventing_one(
    session, garage, garage_schedule, appointment_type, user
):
    # base_price is unset (the fixture default) - the bot must never invent
    # a figure, so it should fall back to a human rather than print "None".
    r = _send(garage, PHONE_RAW, "how much is an MOT", now=_now())
    assert r.intent == "APPOINTMENT_PRICE_QUERY"
    assert "None" not in r.response_text
    assert appointment_type.name in r.response_text


def test_unknown_service_does_not_invent_and_escalates(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r1 = _send(garage, PHONE_RAW, "I need the engine making less angry noises", now=now)
    # Nothing this vague should silently become an MOT booking.
    assert r1.workflow_step != "AWAITING_BOOKING_CONFIRMATION"
    booking_requests = BookingRequest.query.filter_by(garage_id=garage.id).count()
    assert booking_requests == 0


# --------------------------------------------------------------------------
# Cancellation / rescheduling
# --------------------------------------------------------------------------


def test_cancel_flow_identifies_confirms_and_cancels(
    session, garage, garage_schedule, appointment_type, user, customer
):
    customer.phone = PHONE_RAW
    session.commit()
    start = datetime.combine(
        _next_open_weekday(_now()), datetime.min.time(), tzinfo=UTC
    ) + timedelta(hours=10)
    appt = Appointment(
        garage_id=garage.id,
        employee_id=user.id,
        customer_id=customer.id,
        appointment_type_id=appointment_type.id,
        start_time=start,
        end_time=start + timedelta(minutes=60),
        status="BOOKED",
    )
    session.add(appt)
    session.commit()

    now = _now()
    r1 = _send(garage, PHONE_RAW, "I need to cancel my appointment", now=now)
    assert r1.workflow_step == "AWAITING_CANCEL_CONFIRMATION"

    r2 = _send(garage, PHONE_RAW, "yes", now=now)
    assert r2.workflow_step is None
    session.refresh(appt)
    assert appt.status == "CANCELLED"


def test_reschedule_flow_moves_appointment_with_real_availability(
    session, garage, garage_schedule, appointment_type, user, customer
):
    customer.phone = PHONE_RAW
    session.commit()
    now = _now()
    old_day = _next_open_weekday(now)
    start = datetime.combine(old_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
    appt = Appointment(
        garage_id=garage.id,
        employee_id=user.id,
        customer_id=customer.id,
        appointment_type_id=appointment_type.id,
        start_time=start,
        end_time=start + timedelta(minutes=60),
        status="BOOKED",
    )
    session.add(appt)
    session.commit()

    new_day = _next_open_weekday(now, min_days_ahead=5)
    new_day_name = new_day.strftime("%A")

    r1 = _send(garage, PHONE_RAW, "can I move my appointment to " + new_day_name, now=now)
    assert r1.workflow_step == "AWAITING_RESCHEDULE_TIME"

    first_slot = r1.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r2 = _send(garage, PHONE_RAW, first_slot, now=now)
    assert r2.workflow_step == "AWAITING_RESCHEDULE_CONFIRMATION"

    r3 = _send(garage, PHONE_RAW, "yes", now=now)
    assert r3.workflow_step is None
    session.refresh(appt)
    assert appt.start_time.date() == new_day
    assert appt.status == "BOOKED"


# --------------------------------------------------------------------------
# Human handoff / callback
# --------------------------------------------------------------------------


def test_speak_to_human_hands_off_immediately(
    session, garage, garage_schedule, appointment_type, user
):
    r = _send(garage, PHONE_RAW, "can I speak to a person please", now=_now())
    assert r.needs_human is True
    conv_session = ConversationSession.query.filter_by(garage_id=garage.id).one()
    assert conv_session.status == "HUMAN_HANDOFF"
    assert conv_session.handoff_reason


def test_complaint_hands_off(session, garage, garage_schedule, appointment_type, user):
    r = _send(garage, PHONE_RAW, "I want to complain about my last visit", now=_now())
    assert r.needs_human is True


def test_handoff_session_does_not_auto_reply_to_further_messages(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "speak to a human", now=now)
    r2 = _send(garage, PHONE_RAW, "hello?", now=now)
    assert r2.needs_human is True
    assert r2.response_text is None


def test_callback_request_creates_a_record(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r1 = _send(garage, PHONE_RAW, "can someone call me back", now=now)
    assert r1.workflow_step == "AWAITING_CALLBACK_REASON"

    r2 = _send(garage, PHONE_RAW, "my car won't start", now=now)
    assert r2.workflow_step is None
    callback = CallbackRequest.query.filter_by(garage_id=garage.id).one()
    assert callback.phone_number == PHONE_RAW
    assert callback.reason == "my car won't start"
    assert callback.status == "PENDING"


def test_repeated_unresolved_messages_escalate_to_human(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r1 = _send(garage, PHONE_RAW, "asdkjhaskjdh", now=now)
    assert r1.needs_human is False
    r2 = _send(garage, PHONE_RAW, "asdkjhaskjdh again", now=now)
    assert r2.needs_human is True


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------


def test_same_phone_number_never_crosses_tenants(
    session, garage, second_garage, garage_schedule, appointment_type, user
):
    a = Customer(garage_id=garage.id, first_name="Same", last_name="Number", phone=PHONE_RAW)
    b = Customer(
        garage_id=second_garage.id, first_name="Different", last_name="Person", phone=PHONE_RAW
    )
    session.add_all([a, b])
    session.commit()

    now = _now()
    _send(garage, PHONE_RAW, "hello", now=now)
    _send(second_garage, PHONE_RAW, "hello", now=now)

    sessions_a = ConversationSession.query.filter_by(garage_id=garage.id).all()
    sessions_b = ConversationSession.query.filter_by(garage_id=second_garage.id).all()
    assert len(sessions_a) == 1
    assert len(sessions_b) == 1
    assert sessions_a[0].customer_id == a.id
    assert sessions_b[0].customer_id == b.id

    logs_a = CommunicationLog.query.filter_by(garage_id=garage.id).all()
    logs_b = CommunicationLog.query.filter_by(garage_id=second_garage.id).all()
    assert all(log.garage_id == garage.id for log in logs_a)
    assert all(log.garage_id == second_garage.id for log in logs_b)


# --------------------------------------------------------------------------
# Idempotency / duplicate webhook delivery
# --------------------------------------------------------------------------


def test_duplicate_external_message_id_does_not_repeat_the_action(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r1 = _send(
        garage, PHONE_RAW, "can someone call me back", now=now, external_message_id="SM-dup-1"
    )
    assert r1.duplicate is False

    r2 = _send(
        garage, PHONE_RAW, "can someone call me back", now=now, external_message_id="SM-dup-1"
    )
    assert r2.duplicate is True

    # Only the first delivery's turns were logged, and no second callback.
    assert (
        CallbackRequest.query.filter_by(garage_id=garage.id).count() == 0
    )  # still awaiting reason
    logs = CommunicationLog.query.filter_by(garage_id=garage.id, external_id="SM-dup-1").all()
    assert len(logs) == 1


# --------------------------------------------------------------------------
# Session expiry
# --------------------------------------------------------------------------


def test_stale_session_does_not_resume_and_submit_an_old_slot(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    target_day = _next_open_weekday(now)
    weekday_name = target_day.strftime("%A")

    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r3 = _send(garage, PHONE_RAW, weekday_name, now=now)
    assert r3.workflow_step == "AWAITING_TIME"

    much_later = now + timedelta(hours=2)  # past SESSION_TIMEOUT_MINUTES (30)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r4 = _send(garage, PHONE_RAW, first_slot, now=much_later)

    # A stale session starts fresh - "10:30" alone, with no active booking
    # flow, is not understood as a time and is not silently turned into a
    # confirmed slot.
    assert r4.workflow_step != "AWAITING_VEHICLE_REG"
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_expire_stale_sessions_sweep(session, garage, garage_schedule, appointment_type, user):
    now = _now()
    _send(garage, PHONE_RAW, "hello", now=now)
    much_later = now + timedelta(hours=2)

    changed = session_service.expire_stale_sessions(now=much_later)
    assert changed == 1
    conv_session = ConversationSession.query.filter_by(garage_id=garage.id).one()
    assert conv_session.status == "EXPIRED"


# --------------------------------------------------------------------------
# Automation settings default to safe/off
# --------------------------------------------------------------------------


def test_automation_settings_default_to_conversation_automation_disabled(garage):
    settings = automation._automation_settings(garage)
    assert settings.conversation_automation_enabled is False
    assert settings.booking_ack_enabled is True


# --------------------------------------------------------------------------
# Mid-flow corrections / backtracking (issue #62)
#
# A caller can change the day or the service, or ask to go back, at any
# point before the BookingRequest is actually created.
# --------------------------------------------------------------------------


def _first_offered_slot(response_text: str) -> str:
    return response_text.split("have ")[1].split(",")[0].split(" ")[0]


def _session_ctx(garage):
    return (
        ConversationSession.query.filter_by(garage_id=garage.id, customer_phone=PHONE_RAW)
        .one()
        .context
    )


def _book_up_to_time(garage, now):
    """Unrecognised caller, as far as AWAITING_TIME on the first open weekday.
    Returns (first_day, response_at_time_step)."""
    first_day = _next_open_weekday(now)
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r = _send(garage, PHONE_RAW, first_day.strftime("%A"), now=now)
    assert r.workflow_step == "AWAITING_TIME"
    return first_day, r


def test_change_date_after_selecting_a_date(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    first_day, _ = _book_up_to_time(garage, now)
    new_day = _next_open_weekday(now, min_days_ahead=5)
    assert new_day != first_day

    r = _send(garage, PHONE_RAW, f"actually {new_day.strftime('%A')} instead", now=now)

    assert r.workflow_step == "AWAITING_TIME"
    assert new_day.strftime("%A") in r.response_text
    ctx = _session_ctx(garage)
    assert ctx["preferred_date"] == new_day.isoformat()
    assert not ctx.get("preferred_time")


def test_change_date_after_selecting_a_time(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _day, r_time = _book_up_to_time(garage, now)
    r_veh = _send(garage, PHONE_RAW, _first_offered_slot(r_time.response_text), now=now)
    assert r_veh.workflow_step == "AWAITING_VEHICLE_REG"
    assert _session_ctx(garage)["preferred_time"]  # a time is now set

    new_day = _next_open_weekday(now, min_days_ahead=5)
    r = _send(garage, PHONE_RAW, f"can we do {new_day.strftime('%A')} instead", now=now)

    assert r.workflow_step == "AWAITING_TIME"
    ctx = _session_ctx(garage)
    assert ctx["preferred_date"] == new_day.isoformat()
    assert not ctx.get("preferred_time")  # the old time did not survive


def test_change_appointment_type_mid_flow(session, garage, garage_schedule, appointment_type, user):
    service = GarageAppointmentType(
        garage_id=garage.id, name="Service", status="ACTIVE", default_duration_minutes=90
    )
    session.add(service)
    session.commit()

    now = _now()
    _first_day, _ = _book_up_to_time(garage, now)
    assert _session_ctx(garage)["appointment_type_id"] == str(appointment_type.id)

    r = _send(garage, PHONE_RAW, "actually I need a Service instead", now=now)

    ctx = _session_ctx(garage)
    assert ctx["appointment_type_id"] == str(service.id)
    assert not ctx.get("preferred_time")
    assert r.workflow_step in ("AWAITING_TIME", "AWAITING_DATE")


def test_go_back_returns_to_the_previous_step(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _book_up_to_time(garage, now)

    r = _send(garage, PHONE_RAW, "go back", now=now)

    assert r.workflow_step == "AWAITING_DATE"
    ctx = _session_ctx(garage)
    assert not ctx.get("preferred_date")
    assert not ctx.get("preferred_time")


def test_no_stale_time_survives_a_date_change(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _day, r_time = _book_up_to_time(garage, now)
    _send(garage, PHONE_RAW, _first_offered_slot(r_time.response_text), now=now)

    new_day = _next_open_weekday(now, min_days_ahead=5)
    _send(garage, PHONE_RAW, f"actually {new_day.strftime('%A')}", now=now)

    # We are back at the time step with no time chosen - a bare "yes" must
    # not fall through and book anything.
    r = _send(garage, PHONE_RAW, "yes", now=now)
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0
    assert "time" in r.response_text.lower()


def test_final_booking_uses_the_corrected_date(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    first_day, _ = _book_up_to_time(garage, now)
    new_day = _next_open_weekday(now, min_days_ahead=5)
    assert new_day != first_day

    r_time = _send(garage, PHONE_RAW, f"actually {new_day.strftime('%A')} instead", now=now)
    r_veh = _send(garage, PHONE_RAW, _first_offered_slot(r_time.response_text), now=now)
    assert r_veh.workflow_step == "AWAITING_VEHICLE_REG"
    _send(garage, PHONE_RAW, "AB12 CDE", now=now)
    r_done = _send(garage, PHONE_RAW, "yes", now=now)

    assert r_done.workflow_step is None
    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.preferred_date == new_day
    assert booking_request.vehicle_registration == "AB12CDE"
