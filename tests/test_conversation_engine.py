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
    """A Monday 08:00 that is always in the *near future* of the real clock.

    It has to be a fixed weekday so the "which day did you say?" assertions
    don't change meaning with the day the suite runs on - but it must not be a
    fixed *date*. The engine offers slots against the ``now`` injected here,
    while the availability re-check at booking confirmation consults the real
    clock; pin the date and the two eventually disagree. A hard-coded
    2026-09-07 did exactly that: the tests book three days out, so from
    2026-09-10 onwards they were offering slots that the real clock had
    already passed, and the confirmation step answered "that time's no longer
    available".

    Anchoring to the next Monday keeps the offered slots genuinely in the
    future for both clocks, on every day the suite is ever run.
    """
    today = datetime.now(UTC).date()
    # 0 = Monday. `or 7` so "today is Monday" moves to *next* Monday rather
    # than to today, keeping the whole booking window ahead of the real clock.
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    base = datetime(monday.year, monday.month, monday.day, 8, 0, tzinfo=UTC)
    assert base.weekday() == 0
    assert base > datetime.now(UTC)
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


def _existing_appt(session, garage, user, customer, appointment_type, *, day):
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
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
    return appt


@pytest.mark.parametrize(
    "phrase",
    [
        "I need to cancel",
        "I want to cancel please",
        "have to cancel my visit",
        "I no longer need the appointment",
    ],
)
def test_natural_cancel_phrasings_route_to_the_cancel_flow_not_a_fallback(
    session, garage, garage_schedule, appointment_type, user, customer, phrase
):
    customer.phone = PHONE_RAW
    session.commit()
    now = _now()
    _existing_appt(session, garage, user, customer, appointment_type, day=_next_open_weekday(now))

    r = _send(garage, PHONE_RAW, phrase, now=now)
    assert r.intent == "CANCEL_APPOINTMENT"
    assert r.workflow_step == "AWAITING_CANCEL_CONFIRMATION"
    assert r.needs_human is False


@pytest.mark.parametrize(
    "phrase",
    [
        "I can't make tomorrow",
        "I can't make it",
        "need to move my appointment",
        "change my booking",
        "can we rearrange my appointment",
    ],
)
def test_natural_reschedule_phrasings_route_to_the_reschedule_flow(
    session, garage, garage_schedule, appointment_type, user, customer, phrase
):
    customer.phone = PHONE_RAW
    session.commit()
    now = _now()
    _existing_appt(session, garage, user, customer, appointment_type, day=_next_open_weekday(now))

    r = _send(garage, PHONE_RAW, phrase, now=now)
    assert r.intent == "RESCHEDULE_APPOINTMENT"
    assert r.needs_human is False
    # Into the reschedule flow proper - which step depends on whether the
    # phrase also named a day ("...tomorrow") and how many appointments exist.
    assert r.workflow_step and r.workflow_step.startswith("AWAITING_RESCHEDULE")


def test_multiple_appointments_are_disambiguated_before_a_cancel(
    session, garage, garage_schedule, appointment_type, user, customer
):
    customer.phone = PHONE_RAW
    session.commit()
    now = _now()
    _existing_appt(session, garage, user, customer, appointment_type, day=_next_open_weekday(now))
    _existing_appt(
        session,
        garage,
        user,
        customer,
        appointment_type,
        day=_next_open_weekday(now, min_days_ahead=6),
    )

    r1 = _send(garage, PHONE_RAW, "I need to cancel my appointment", now=now)
    assert r1.workflow_step == "AWAITING_CANCEL_CHOICE"
    assert "1." in r1.response_text and "2." in r1.response_text

    r2 = _send(garage, PHONE_RAW, "1", now=now)
    assert r2.workflow_step == "AWAITING_CANCEL_CONFIRMATION"  # still confirms, never blind-cancels


def test_cancel_interrupts_an_in_progress_booking(
    session, garage, garage_schedule, appointment_type, user, customer
):
    customer.phone = PHONE_RAW
    session.commit()
    now = _now()
    _existing_appt(session, garage, user, customer, appointment_type, day=_next_open_weekday(now))

    # Part-way through booking a *new* appointment...
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    assert _session(garage).workflow_step == "AWAITING_DATE"

    # ...the customer pivots to cancelling their existing one.
    r = _send(garage, PHONE_RAW, "actually I need to cancel my appointment", now=now)
    assert r.intent == "CANCEL_APPOINTMENT"
    assert r.workflow_step == "AWAITING_CANCEL_CONFIRMATION"


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
    # Default CONVERSATION_MAX_UNRESOLVED_TURNS is 3 - two misses still get a
    # re-prompt, only the third hands off.
    r1 = _send(garage, PHONE_RAW, "asdkjhaskjdh", now=now)
    assert r1.needs_human is False
    r2 = _send(garage, PHONE_RAW, "asdkjhaskjdh again", now=now)
    assert r2.needs_human is False
    r3 = _send(garage, PHONE_RAW, "still asdkjhaskjdh", now=now)
    assert r3.needs_human is True


def test_unresolved_escalation_threshold_is_configurable(
    app, monkeypatch, session, garage, garage_schedule, appointment_type, user
):
    monkeypatch.setitem(app.config, "CONVERSATION_MAX_UNRESOLVED_TURNS", 2)
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

    much_later = now + timedelta(hours=13)  # past the 12h WhatsApp idle window
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r4 = _send(garage, PHONE_RAW, first_slot, now=much_later)

    # A stale session starts fresh - "10:30" alone, with no active booking
    # flow, is not understood as a time and is not silently turned into a
    # confirmed slot.
    assert r4.workflow_step != "AWAITING_VEHICLE_REG"
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_whatsapp_session_resumes_within_the_idle_window(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r3 = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), now=now)
    assert r3.workflow_step == "AWAITING_TIME"

    # Six hours later (well inside the 12h WhatsApp window) the same thread
    # picks up where it left off.
    later = now + timedelta(hours=6)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    r4 = _send(garage, PHONE_RAW, first_slot, now=later)
    assert r4.workflow_step == "AWAITING_VEHICLE_REG"


def test_expire_stale_sessions_sweep(session, garage, garage_schedule, appointment_type, user):
    now = _now()
    _send(garage, PHONE_RAW, "I want to book an appointment", now=now)
    much_later = now + timedelta(hours=13)

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


# --------------------------------------------------------------------------
# Ambiguous booking must clarify, not hand off (issue #71)
# --------------------------------------------------------------------------


def test_ambiguous_booking_acronym_asks_which_service_not_handoff(
    session, garage, garage_schedule, appointment_type, user
):
    r = _send(garage, PHONE_RAW, "Can I make a MSC booking?", now=_now())

    assert r.needs_human is False
    assert r.workflow_step == "AWAITING_TYPE"
    assert appointment_type.name in r.response_text
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_vague_booking_request_asks_what_service(
    session, garage, garage_schedule, appointment_type, user
):
    r = _send(garage, PHONE_RAW, "I want to book something", now=_now())

    assert r.needs_human is False
    assert r.workflow_step == "AWAITING_TYPE"


def test_unknown_service_name_clarifies_up_to_threshold_then_hands_off(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    # Default threshold is 3 clarification rounds.
    r0 = _send(garage, PHONE_RAW, "I want to book a flurble", now=now)
    assert (r0.needs_human, r0.workflow_step) == (False, "AWAITING_TYPE")
    r1 = _send(garage, PHONE_RAW, "a flurble", now=now)
    assert (r1.needs_human, r1.workflow_step) == (False, "AWAITING_TYPE")
    r2 = _send(garage, PHONE_RAW, "still a flurble", now=now)
    assert (r2.needs_human, r2.workflow_step) == (False, "AWAITING_TYPE")

    r3 = _send(garage, PHONE_RAW, "flurble please", now=now)
    assert r3.needs_human is True
    assert r3.response_text  # a spoken handoff line, never a bare end
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_a_matched_service_clears_the_clarification_counter(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "book a flurble", now=now)
    _send(garage, PHONE_RAW, "another flurble", now=now)
    r = _send(garage, PHONE_RAW, appointment_type.name, now=now)
    assert r.needs_human is False
    assert r.workflow_step in ("AWAITING_NAME", "AWAITING_DATE")


# --------------------------------------------------------------------------
# Voice HUMAN_HANDOFF must not trap future calls (issue #71)
# --------------------------------------------------------------------------


def test_voice_handoff_session_expires_so_a_later_call_starts_fresh(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r1 = _send(garage, PHONE_RAW, "I want to speak to someone", channel="VOICE", now=now)
    assert r1.needs_human is True
    sess = ConversationSession.query.filter_by(
        garage_id=garage.id, channel="VOICE", customer_phone=PHONE_RAW
    ).one()
    assert sess.status == "HUMAN_HANDOFF"

    later = now + timedelta(minutes=session_service.SESSION_TIMEOUT_MINUTES + 5)
    r2 = _send(garage, PHONE_RAW, "what are your opening hours", channel="VOICE", now=later)

    assert r2.needs_human is False
    assert r2.response_text and "hours" in r2.response_text.lower()
    sessions = ConversationSession.query.filter_by(
        garage_id=garage.id, channel="VOICE", customer_phone=PHONE_RAW
    ).all()
    # The trapped HUMAN_HANDOFF session was retired; a fresh one handled the call.
    assert len(sessions) == 2
    assert {s.status for s in sessions} == {"EXPIRED", "COMPLETED"}
    assert sess.status == "EXPIRED"


def test_whatsapp_handoff_session_never_expires(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I want to speak to someone", channel="WHATSAPP", now=now)
    later = now + timedelta(hours=6)
    r = _send(garage, PHONE_RAW, "hello again", channel="WHATSAPP", now=later)

    assert r.needs_human is True
    assert r.response_text is None


def test_expire_stale_sessions_sweeps_a_stale_voice_handoff(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I want to speak to someone", channel="VOICE", now=now)
    later = now + timedelta(hours=2)

    changed = session_service.expire_stale_sessions(now=later)

    assert changed == 1
    sess = ConversationSession.query.filter_by(garage_id=garage.id, channel="VOICE").one()
    assert sess.status == "EXPIRED"


# --------------------------------------------------------------------------
# WhatsApp automation quality (issue #73)
# --------------------------------------------------------------------------


def test_bare_greeting_gets_a_capability_intro(
    session, garage, garage_schedule, appointment_type, user
):
    r = _send(garage, PHONE_RAW, "hi there", now=_now())
    assert r.intent == "GREETING"
    assert r.needs_human is False
    body = r.response_text.lower()
    assert garage.name.lower() in body
    assert "book" in body and "cancel" in body


def test_greeting_with_a_request_routes_to_the_request(
    session, garage, garage_schedule, appointment_type, user
):
    r = _send(garage, PHONE_RAW, "hi, can I book an MOT please", now=_now())
    assert r.intent == "CREATE_BOOKING"
    assert r.workflow_step in ("AWAITING_NAME", "AWAITING_DATE")


def test_thanks_gets_a_polite_close(session, garage, garage_schedule, appointment_type, user):
    r = _send(garage, PHONE_RAW, "thanks very much!", now=_now())
    assert r.intent == "SMALL_TALK"
    assert r.needs_human is False
    assert r.workflow_step is None


def test_whatsapp_shorthand_is_understood(session, garage, garage_schedule, appointment_type, user):
    r = _send(garage, PHONE_RAW, "can u book me an appt for tmrw pls", now=_now())
    assert r.intent == "CREATE_BOOKING"


def test_natural_yes_confirms_a_booking(session, garage, garage_schedule, appointment_type, user):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r3 = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), now=now)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    _send(garage, PHONE_RAW, first_slot, now=now)
    _send(garage, PHONE_RAW, "AB12 CDE", now=now)

    r = _send(garage, PHONE_RAW, "yeah go ahead", now=now)
    assert r.workflow_step is None
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 1


def test_whatsapp_booking_confirmation_includes_the_reference(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", now=now)
    r3 = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), now=now)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    _send(garage, PHONE_RAW, first_slot, now=now)
    _send(garage, PHONE_RAW, "AB12 CDE", now=now)
    r = _send(garage, PHONE_RAW, "yes", now=now)

    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.booking_reference
    assert booking_request.booking_reference in r.response_text


def test_voice_booking_confirmation_omits_the_reference(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", channel="VOICE", now=now)
    _send(garage, PHONE_RAW, "Jane Doe", channel="VOICE", now=now)
    r3 = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), channel="VOICE", now=now)
    first_slot = r3.response_text.split("have ")[1].split(",")[0].split(" ")[0]
    _send(garage, PHONE_RAW, first_slot, channel="VOICE", now=now)
    _send(garage, PHONE_RAW, "AB12 CDE", channel="VOICE", now=now)
    r = _send(garage, PHONE_RAW, "yes", channel="VOICE", now=now)

    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.booking_reference  # still created
    assert booking_request.booking_reference not in r.response_text  # just not read out


def test_voice_session_still_expires_at_thirty_minutes(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", channel="VOICE", now=now)
    r = _send(garage, PHONE_RAW, "Jane Doe", channel="VOICE", now=now + timedelta(minutes=45))
    # 45 min > the 30-min voice window: the follow-up starts a fresh session,
    # so "Jane Doe" is not read as the name step of the old booking flow.
    assert r.workflow_step != "AWAITING_DATE"


# --------------------------------------------------------------------------
# Interruptible workflows: topic switch, navigation, multi-intent (issue #75)
# --------------------------------------------------------------------------


def _session(garage, phone=PHONE_RAW):
    return ConversationSession.query.filter_by(garage_id=garage.id, customer_phone=phone).one()


def _to_awaiting_date(garage, now):
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    r = _send(garage, PHONE_RAW, "Jane Doe", now=now)
    assert r.workflow_step == "AWAITING_DATE"


def test_booking_interrupted_by_hours_question_then_resumes(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)

    r = _send(garage, PHONE_RAW, "what time do you close?", now=now)
    assert r.needs_human is False
    assert "17:00" in r.response_text  # answered the actual question
    assert "carry on" in r.response_text.lower()  # offered to resume
    sess = _session(garage)
    assert sess.workflow_step == "AWAITING_RESUME"
    assert sess.context["resume_step"] == "AWAITING_DATE"

    # Answering the day directly resumes the booking.
    r2 = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), now=now)
    assert r2.workflow_step == "AWAITING_TIME"


def test_booking_interrupted_by_price_question(
    session, garage, garage_schedule, appointment_type, user
):
    appointment_type.base_price = Decimal("54.85")
    session.commit()
    now = _now()
    _to_awaiting_date(garage, now)

    r = _send(garage, PHONE_RAW, "how much is an MOT?", now=now)
    assert r.needs_human is False
    assert "£" in r.response_text
    assert _session(garage).workflow_step == "AWAITING_RESUME"

    r2 = _send(garage, PHONE_RAW, "yes carry on", now=now)
    assert r2.workflow_step == "AWAITING_DATE"


def test_multi_intent_faq_answered_in_one_reply(
    session, garage, garage_schedule, appointment_type, user
):
    garage.address = "1 Test Street"
    garage.postcode = "TE1 1ST"
    appointment_type.base_price = Decimal("54.85")
    session.commit()

    r = _send(garage, PHONE_RAW, "what are your prices, opening hours and address?", now=_now())
    body = r.response_text
    assert "17:00" in body  # hours
    assert "TE1 1ST" in body  # address
    assert "£" in body  # price
    assert r.needs_human is False


def test_multi_intent_faq_mid_booking_then_resume(
    session, garage, garage_schedule, appointment_type, user
):
    appointment_type.base_price = Decimal("54.85")
    session.commit()
    now = _now()
    _send(garage, PHONE_RAW, "can I book an appointment please", now=now)
    assert _session(garage).workflow_step == "AWAITING_TYPE"

    # The exact production message that used to loop "which service?".
    r = _send(
        garage,
        PHONE_RAW,
        "actually answer questions about prices opening hours and where you are",
        now=now,
    )
    assert r.needs_human is False
    assert "17:00" in r.response_text
    assert "£" in r.response_text
    assert _session(garage).workflow_step == "AWAITING_RESUME"


def test_start_again_resets_a_half_filled_booking(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)

    r = _send(garage, PHONE_RAW, "start again", now=now)
    assert r.needs_human is False
    sess = _session(garage)
    assert sess.workflow_step is None
    assert sess.context == {}


def test_back_to_the_beginning_is_never_matched_to_a_service(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _send(garage, PHONE_RAW, "can I book an appointment please", now=now)  # AWAITING_TYPE

    r = _send(garage, PHONE_RAW, "back to the beginning", now=now)
    assert r.needs_human is False  # did NOT escalate
    assert "which" not in r.response_text.lower()  # not "which service?"
    assert _session(garage).workflow_step is None
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_cancel_that_mid_booking_stops_the_flow(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)

    r = _send(garage, PHONE_RAW, "cancel that", now=now)
    assert r.needs_human is False
    assert "nothing has been booked" in r.response_text.lower()
    # A fresh message afterwards starts clean, not mid-booking.
    r2 = _send(garage, PHONE_RAW, "what are your opening hours", now=now)
    assert r2.workflow_step is None
    assert "17:00" in r2.response_text


def test_never_mind_mid_booking_stops_the_flow(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)
    r = _send(garage, PHONE_RAW, "never mind", now=now)
    assert r.needs_human is False
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_go_back_from_time_returns_to_the_date_step(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)
    r = _send(garage, PHONE_RAW, _next_open_weekday(now).strftime("%A"), now=now)
    assert r.workflow_step == "AWAITING_TIME"

    r2 = _send(garage, PHONE_RAW, "go back", now=now)
    assert r2.needs_human is False
    assert r2.workflow_step == "AWAITING_DATE"


def test_correction_of_the_service_still_works_mid_flow(
    session, garage, garage_schedule, appointment_type, user
):
    service = GarageAppointmentType(
        garage_id=garage.id, name="Service", status="ACTIVE", default_duration_minutes=90
    )
    session.add(service)
    session.commit()

    now = _now()
    _to_awaiting_date(garage, now)
    r = _send(garage, PHONE_RAW, "actually I need a Service instead", now=now)
    assert r.needs_human is False
    assert _session(garage).context["appointment_type_id"] == str(service.id)


def test_one_odd_message_mid_booking_does_not_hand_off(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    _to_awaiting_date(garage, now)
    r = _send(garage, PHONE_RAW, "asdkjhaskjdh", now=now)
    assert r.needs_human is False  # clarify, don't escalate


# --------------------------------------------------------------------------
# Audit regressions - the +44 7925 392354 conversation
# --------------------------------------------------------------------------


def _known(session, customer):
    customer.phone = PHONE_RAW
    session.commit()
    return customer


def test_book_for_24th_september_stays_24_september(
    session, garage, garage_schedule, appointment_type, user, customer
):
    """The exact failure: 'book for the 24th September' was turned into the
    next Tuesday (15 September)."""
    _known(session, customer)
    now = _now()  # Monday 7 September 2026

    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    r = _send(garage, PHONE_RAW, "the 24th september", now=now)

    assert r.needs_human is False
    assert "24 September" in r.response_text
    assert "15 September" not in r.response_text
    assert r.workflow_step in {"AWAITING_TIME", "AWAITING_DATE"}  # times, or "nothing that day"
    # If slots were offered, the stored preferred_date is the 24th.
    if r.workflow_step == "AWAITING_TIME":
        assert _session(garage).context["preferred_date"] == "2026-09-24"


def test_tuesday_24th_september_is_the_24th_not_the_next_tuesday(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()

    r = _send(garage, PHONE_RAW, "can i book an MOT for Tuesday 24th september", now=now)

    assert r.needs_human is False
    assert "24 September" in r.response_text
    assert "15 September" not in r.response_text
    if r.workflow_step == "AWAITING_TIME":
        assert _session(garage).context["preferred_date"] == "2026-09-24"


def test_a_date_more_than_seven_days_out_is_accepted(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()
    far = _next_open_weekday(now, min_days_ahead=21)  # ~3 weeks out
    phrase = f"{far.day} {far.strftime('%B')}"  # e.g. "28 September"

    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    r = _send(garage, PHONE_RAW, phrase, now=now)

    assert r.needs_human is False
    # Not a re-ask, not "we can only book up to ...".
    assert "what day" not in r.response_text.lower()
    assert "only take bookings up to" not in r.response_text.lower()
    assert (
        far.strftime("%d %B").lstrip("0") in r.response_text
        or far.strftime("%B") in r.response_text
    )


def test_numeric_date_format_is_understood(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    r = _send(garage, PHONE_RAW, "24/09", now=now)
    assert r.needs_human is False
    assert "24 September" in r.response_text


def test_customer_can_correct_their_name_and_it_updates_the_record(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()

    r1 = _send(garage, PHONE_RAW, "you've spelt my name wrong, it should be Jonathan Reed", now=now)
    assert r1.needs_human is False
    assert r1.workflow_step == "AWAITING_NAME_CORRECTION_CONFIRM"
    assert "Jonathan Reed" in r1.response_text

    r2 = _send(garage, PHONE_RAW, "yes", now=now)
    assert r2.needs_human is False
    session.refresh(customer)
    assert (customer.first_name, customer.last_name) == ("Jonathan", "Reed")


def test_name_correction_asks_for_the_name_when_not_given_inline(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()
    r1 = _send(garage, PHONE_RAW, "can you change my name?", now=now)
    assert r1.workflow_step == "AWAITING_NAME_CORRECTION"
    r2 = _send(garage, PHONE_RAW, "Jonathan Reed", now=now)
    assert r2.workflow_step == "AWAITING_NAME_CORRECTION_CONFIRM"
    _send(garage, PHONE_RAW, "yes please", now=now)
    session.refresh(customer)
    assert customer.first_name == "Jonathan"


def test_name_correction_from_an_unrecognised_number_hands_off(
    session, garage, garage_schedule, appointment_type, user
):
    now = _now()
    r = _send(garage, "+447999000123", "my name is wrong, change it to Sam Blake", now=now)
    assert r.needs_human is True
    assert r.workflow_step is None


def test_switching_from_booking_to_name_correction_midflow(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()
    _send(garage, PHONE_RAW, "I need an MOT", now=now)
    assert _session(garage).workflow_step == "AWAITING_DATE"

    r = _send(garage, PHONE_RAW, "wait, you've got my name wrong", now=now)
    assert r.needs_human is False
    assert r.workflow_step in {"AWAITING_NAME_CORRECTION", "AWAITING_NAME_CORRECTION_CONFIRM"}


def test_change_email_is_explained_and_handed_off_not_looped_into_booking(
    session, garage, garage_schedule, appointment_type, user, customer
):
    _known(session, customer)
    now = _now()
    r = _send(garage, PHONE_RAW, "can you change my email address please", now=now)
    assert r.needs_human is True
    assert "which service" not in r.response_text.lower()
    assert "what day" not in r.response_text.lower()
    assert "security" in r.response_text.lower() or "can't change" in r.response_text.lower()
