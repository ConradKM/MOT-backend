"""app/ai_voice/tools.py - the tool schemas and tenant-scoped dispatcher the
OpenAI Realtime model calls during a phone call.
"""

import json
from datetime import UTC, datetime, time, timedelta

from app.ai_voice.instructions import build_instructions
from app.ai_voice.tools import TOOL_SCHEMAS, VoiceToolState, dispatch_tool
from app.conversation import actions
from app.models.ai_voice_faq import GarageVoiceFAQ
from app.models.booking_request import BookingRequest


def _future_weekday(days_ahead=7):
    d = datetime.now(UTC).date() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_tool_schemas_cover_the_required_tools():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "get_business_info",
        "get_appointment_types",
        "get_available_slots",
        "get_business_faqs",
        "create_booking",
        "get_my_appointments",
        "cancel_appointment",
        "reschedule_appointment",
        "request_human_handoff",
    }
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        assert schema.get("description")
        assert schema["parameters"]["type"] == "object"


def test_get_business_info_returns_real_data(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_business_info", "{}"))
    assert result["ok"] is True
    assert result["name"] == garage.name
    assert result["phone"] == garage.phone
    assert result["address"] == garage.address
    assert len(result["opening_hours"]) == 7


def test_get_appointment_types_lists_only_active_types(session, garage, appointment_type):
    from app.models.appointments.appointment_type import GarageAppointmentType

    hidden = GarageAppointmentType(garage_id=garage.id, name="Old Service", status="HIDDEN")
    session.add(hidden)
    session.commit()

    result = json.loads(dispatch_tool(garage, "+447123456789", "get_appointment_types", "{}"))
    assert result["ok"] is True
    names = {t["name"] for t in result["appointment_types"]}
    assert names == {appointment_type.name}


def test_get_available_slots_reports_real_open_times(garage, garage_schedule, appointment_type):
    day = _future_weekday()
    args = json.dumps({"appointment_type_id": str(appointment_type.id), "date": day.isoformat()})
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_available_slots", args))
    assert result["ok"] is True
    assert result["is_open"] is True
    assert "09:00" in result["available_times"]


def test_get_available_slots_never_claims_a_full_slot_is_available(
    garage, garage_schedule, appointment_type, make_appointment
):
    day = _future_weekday()
    # One employee is the fixture's real capacity. A real appointment fills
    # 09:00, so the voice payload must not offer it merely because the garage
    # is open that day.
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9)
    make_appointment(start)
    args = json.dumps({"appointment_type_id": str(appointment_type.id), "date": day.isoformat()})
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_available_slots", args))
    assert "09:00" not in result["available_times"]


def test_create_booking_records_the_selected_real_slot(garage, garage_schedule, appointment_type):
    day = _future_weekday()
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    result = json.loads(dispatch_tool(garage, "+447123456789", "create_booking", args))
    booking = BookingRequest.query.filter_by(booking_reference=result["booking_reference"]).one()
    assert result["status"] == "PENDING"
    assert (booking.preferred_date, booking.preferred_time.strftime("%H:%M")) == (day, "09:00")


def test_production_voice_booking_requires_a_slot_from_this_calls_live_lookup(
    garage, garage_schedule, appointment_type
):
    day = _future_weekday()
    create_args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    state = VoiceToolState()
    rejected = json.loads(
        dispatch_tool(garage, "+447123456789", "create_booking", create_args, state=state)
    )
    assert rejected["ok"] is False
    assert BookingRequest.query.count() == 0

    lookup_args = json.dumps(
        {"appointment_type_id": str(appointment_type.id), "date": day.isoformat()}
    )
    lookup = json.loads(
        dispatch_tool(garage, "+447123456789", "get_available_slots", lookup_args, state=state)
    )
    assert "09:00" in lookup["available_times"]
    created = json.loads(
        dispatch_tool(garage, "+447123456789", "create_booking", create_args, state=state)
    )
    assert created["ok"] is True


def test_voice_booking_tool_id_is_idempotent_across_controller_reconnects(
    garage, garage_schedule, appointment_type
):
    day = _future_weekday()
    lookup_args = json.dumps(
        {"appointment_type_id": str(appointment_type.id), "date": day.isoformat()}
    )
    create_args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    first_state = VoiceToolState()
    dispatch_tool(garage, "+447123456789", "get_available_slots", lookup_args, state=first_state)
    first = json.loads(
        dispatch_tool(
            garage,
            "+447123456789",
            "create_booking",
            create_args,
            state=first_state,
            tool_call_id="openai_tool_1",
        )
    )
    retry_state = VoiceToolState()
    dispatch_tool(garage, "+447123456789", "get_available_slots", lookup_args, state=retry_state)
    second = json.loads(
        dispatch_tool(
            garage,
            "+447123456789",
            "create_booking",
            create_args,
            state=retry_state,
            tool_call_id="openai_tool_1",
        )
    )
    assert second["booking_reference"] == first["booking_reference"]
    assert BookingRequest.query.filter_by(voice_tool_call_id="openai_tool_1").count() == 1


def test_repeated_book_it_calls_create_at_most_one_request_per_voice_call(
    garage, garage_schedule, appointment_type
):
    day = _future_weekday()
    lookup_args = json.dumps(
        {"appointment_type_id": str(appointment_type.id), "date": day.isoformat()}
    )
    create_args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    state = VoiceToolState()
    dispatch_tool(garage, "+447123456789", "get_available_slots", lookup_args, state=state)
    first = json.loads(
        dispatch_tool(
            garage,
            "+447123456789",
            "create_booking",
            create_args,
            state=state,
            tool_call_id="openai_tool_first",
            call_id="rtc_repeat",
        )
    )
    repeated = json.loads(
        dispatch_tool(
            garage,
            "+447123456789",
            "create_booking",
            create_args,
            state=state,
            tool_call_id="openai_tool_second",
            call_id="rtc_repeat",
        )
    )
    assert repeated["booking_reference"] == first["booking_reference"]
    assert BookingRequest.query.filter_by(voice_call_id="rtc_repeat").count() == 1


def test_live_slot_proof_cannot_be_reused_for_a_different_service_or_date(
    session, garage, garage_schedule, appointment_type
):
    from app.models.appointments.appointment_type import GarageAppointmentType

    other = GarageAppointmentType(garage_id=garage.id, name="Long service", status="ACTIVE")
    session.add(other)
    session.commit()
    day = _future_weekday()
    state = VoiceToolState()
    lookup_args = json.dumps(
        {"appointment_type_id": str(appointment_type.id), "date": day.isoformat()}
    )
    dispatch_tool(garage, "+447123456789", "get_available_slots", lookup_args, state=state)
    args = json.dumps(
        {
            "appointment_type_id": str(other.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    assert (
        json.loads(dispatch_tool(garage, "+447123456789", "create_booking", args, state=state))[
            "ok"
        ]
        is False
    )


def test_get_available_slots_unknown_type_is_a_clean_error(garage):
    args = json.dumps({"appointment_type_id": "not-a-real-id", "date": "2026-01-01"})
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_available_slots", args))
    assert result["ok"] is False


def test_create_booking_creates_a_real_pending_request(garage, garage_schedule, appointment_type):
    day = _future_weekday()
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    result = json.loads(dispatch_tool(garage, "+447123456789", "create_booking", args))
    assert result["ok"] is True
    booking = BookingRequest.query.filter_by(booking_reference=result["booking_reference"]).one()
    assert booking.status == "PENDING"
    assert booking.customer_phone == "+447123456789"
    assert booking.source == "CONVERSATION"


def test_create_booking_uses_caller_phone_when_none_given(
    garage, garage_schedule, appointment_type
):
    day = _future_weekday()
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:30",
            "first_name": "Sam",
            "last_name": "Ridley",
            "vehicle_registration": "AB12 CDE",
        }
    )
    result = json.loads(dispatch_tool(garage, "+447987654321", "create_booking", args))
    assert result["ok"] is True
    booking = BookingRequest.query.filter_by(booking_reference=result["booking_reference"]).one()
    assert booking.customer_phone == "+447987654321"


def test_create_booking_rejects_a_slot_that_is_no_longer_available(
    garage, garage_schedule, appointment_type
):
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": "2020-01-01",  # long past - never bookable
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "vehicle_registration": "PB11 REQ",
        }
    )
    result = json.loads(dispatch_tool(garage, "+447123456789", "create_booking", args))
    assert result["ok"] is False


def test_business_faqs_are_tenant_scoped_and_exclude_disabled_and_archived(
    session, garage, second_garage
):
    session.add_all(
        [
            GarageVoiceFAQ(garage_id=garage.id, question="Can I wait?", answer="Yes."),
            GarageVoiceFAQ(
                garage_id=garage.id, question="Disabled", answer="No.", is_enabled=False
            ),
            GarageVoiceFAQ(garage_id=second_garage.id, question="Other tenant", answer="Leak."),
        ]
    )
    session.commit()
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_business_faqs", "{}"))
    assert result == {"ok": True, "faqs": [{"question": "Can I wait?", "answer": "Yes."}]}


def test_voice_instructions_require_tools_for_unknown_and_authoritative_answers(garage):
    instructions = build_instructions(garage)
    # This is the guardrail against a model treating arbitrary FAQ prose as a
    # source for a price, a slot, or a made-up policy answer.
    assert "Call the matching tool immediately before answering" in instructions
    assert "FAQs are not operational data and must never override" in instructions
    assert (
        "If no authoritative tool result or FAQ answers the question, say clearly" in instructions
    )


def test_request_human_handoff_creates_a_callback(garage):
    args = json.dumps({"reason": "Caller asked about a complex insurance claim."})
    result = json.loads(dispatch_tool(garage, "+447123456789", "request_human_handoff", args))
    assert result["ok"] is True

    from app.extensions import db
    from app.models.conversation.callback_request import CallbackRequest

    callback = db.session.get(CallbackRequest, result["callback_id"])
    assert callback.garage_id == garage.id
    assert callback.phone_number == "+447123456789"
    assert "insurance" in callback.reason


def test_get_my_appointments_lists_the_callers_own_upcoming_appointments(
    garage, customer, make_appointment
):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    make_appointment(start)
    result = json.loads(dispatch_tool(garage, customer.phone, "get_my_appointments", "{}"))
    assert result["ok"] is True
    assert len(result["appointments"]) == 1
    assert result["appointments"][0]["service"] == "MOT"


def test_get_my_appointments_is_empty_for_an_unknown_number(garage):
    result = json.loads(dispatch_tool(garage, "+447000000000", "get_my_appointments", "{}"))
    assert result["ok"] is True
    assert result["appointments"] == []


def test_appointment_lookup_cannot_be_redirected_to_another_callers_number(
    garage, customer, make_appointment
):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    make_appointment(start)
    # Prompt injection or a caller simply supplying somebody else's number
    # must not turn the caller-scoped lookup into an account lookup.
    result = json.loads(
        dispatch_tool(
            garage,
            "+447000000000",
            "get_my_appointments",
            json.dumps({"phone": customer.phone}),
        )
    )
    assert result == {"ok": True, "appointments": []}


def test_appointment_cancellation_cannot_be_redirected_to_another_number(
    garage, customer, make_appointment
):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appointment = make_appointment(start)
    result = json.loads(
        dispatch_tool(
            garage,
            "+447000000000",
            "cancel_appointment",
            json.dumps({"appointment_id": str(appointment.id), "phone": customer.phone}),
        )
    )
    assert result["ok"] is False
    assert appointment.status == "BOOKED"


def test_booking_contact_number_does_not_link_another_customers_record(
    session, garage, garage_schedule, appointment_type, customer
):
    customer.phone = "+447123456789"
    session.commit()
    day = _future_weekday()
    args = json.dumps(
        {
            "appointment_type_id": str(appointment_type.id),
            "date": day.isoformat(),
            "time": "09:00",
            "first_name": "Alex",
            "last_name": "Turner",
            "phone": customer.phone,
            "vehicle_registration": "PB11 REQ",
        }
    )
    result = json.loads(dispatch_tool(garage, "+447000000000", "create_booking", args))
    booking = BookingRequest.query.filter_by(booking_reference=result["booking_reference"]).one()
    assert booking.customer_id is None
    assert booking.customer_phone == customer.phone


def test_cancel_appointment_cancels_the_callers_own_appointment(garage, customer, make_appointment):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appt = make_appointment(start)
    args = json.dumps({"appointment_id": str(appt.id), "confirmed": True})
    result = json.loads(dispatch_tool(garage, customer.phone, "cancel_appointment", args))
    assert result["ok"] is True
    assert appt.status == "CANCELLED"


def test_cancel_appointment_rejects_another_customers_appointment(
    session, garage, customer, make_appointment
):
    from app.models.customer import Customer

    other = Customer(
        garage_id=garage.id, first_name="Someone", last_name="Else", phone="+447999999999"
    )
    session.add(other)
    session.commit()

    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appt = make_appointment(start)  # belongs to `customer`, not `other`
    args = json.dumps({"appointment_id": str(appt.id), "confirmed": True})
    result = json.loads(dispatch_tool(garage, other.phone, "cancel_appointment", args))
    assert result["ok"] is False
    assert appt.status == "BOOKED"


def test_voice_cannot_mutate_a_no_show_appointment(session, garage, customer, make_appointment):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appointment = make_appointment(start)
    appointment.status = "NO_SHOW"
    session.commit()

    cancelled, cancel_reason = actions.cancel_appointment(garage, appointment)
    moved, move_reason = actions.reschedule_appointment(
        garage, appointment, _future_weekday(days_ahead=10), time(10, 0)
    )

    assert (cancelled, cancel_reason) == (False, "terminal_appointment")
    assert (moved, move_reason) == (False, "terminal_appointment")
    assert appointment.status == "NO_SHOW"


def test_reschedule_appointment_moves_the_callers_own_appointment(
    garage, garage_schedule, customer, make_appointment
):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appt = make_appointment(start)
    new_day = _future_weekday(days_ahead=10)
    args = json.dumps(
        {
            "appointment_id": str(appt.id),
            "date": new_day.isoformat(),
            "time": "10:00",
            "confirmed": True,
        }
    )
    result = json.loads(dispatch_tool(garage, customer.phone, "reschedule_appointment", args))
    assert result["ok"] is True
    assert appt.start_time.date() == new_day
    assert appt.start_time.strftime("%H:%M") == "10:00"


def test_reschedule_uses_the_existing_appointments_duration_not_edited_service(
    session, garage, garage_schedule, appointment_type, make_appointment
):
    old_start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appointment = make_appointment(old_start, minutes=90)
    # This is a catalogue edit for future customers, not a rewrite of the
    # appointment's actual 90-minute duration.
    appointment_type.default_duration_minutes = 30
    session.commit()

    moved, reason = actions.reschedule_appointment(
        garage,
        appointment,
        _future_weekday(days_ahead=10),
        time(16, 0),
    )

    # The default schedule closes at 17:00: the outdated 30-minute type
    # would appear to fit, but the actual appointment ends at 17:30.
    assert moved is False
    assert reason == "outside_hours"
    assert appointment.start_time == old_start


def test_live_voice_appointment_mutation_requires_lookup_and_confirmation(
    garage, garage_schedule, customer, make_appointment, appointment_type
):
    appt = make_appointment(datetime.now(UTC).replace(microsecond=0) + timedelta(days=3))
    state = VoiceToolState()
    cancel_args = json.dumps({"appointment_id": str(appt.id), "confirmed": True})
    not_looked_up = json.loads(
        dispatch_tool(garage, customer.phone, "cancel_appointment", cancel_args, state=state)
    )
    assert not_looked_up["ok"] is False
    assert appt.status == "BOOKED"

    lookup = json.loads(
        dispatch_tool(garage, customer.phone, "get_my_appointments", "{}", state=state)
    )
    assert str(appt.id) in {item["id"] for item in lookup["appointments"]}
    not_confirmed = json.loads(
        dispatch_tool(
            garage,
            customer.phone,
            "cancel_appointment",
            json.dumps({"appointment_id": str(appt.id), "confirmed": False}),
            state=state,
        )
    )
    assert not_confirmed["ok"] is False
    confirmed = json.loads(
        dispatch_tool(garage, customer.phone, "cancel_appointment", cancel_args, state=state)
    )
    assert confirmed["ok"] is True


def test_live_voice_reschedule_requires_live_lookup_and_returned_slot(
    garage, garage_schedule, customer, make_appointment, appointment_type
):
    appt = make_appointment(datetime.now(UTC).replace(microsecond=0) + timedelta(days=3))
    day = _future_weekday(days_ahead=10)
    state = VoiceToolState()
    dispatch_tool(garage, customer.phone, "get_my_appointments", "{}", state=state)
    args = json.dumps(
        {
            "appointment_id": str(appt.id),
            "date": day.isoformat(),
            "time": "10:00",
            "confirmed": True,
        }
    )
    unchecked = json.loads(
        dispatch_tool(garage, customer.phone, "reschedule_appointment", args, state=state)
    )
    assert unchecked["ok"] is False
    dispatch_tool(
        garage,
        customer.phone,
        "get_available_slots",
        json.dumps({"appointment_type_id": str(appointment_type.id), "date": day.isoformat()}),
        state=state,
    )
    moved = json.loads(
        dispatch_tool(garage, customer.phone, "reschedule_appointment", args, state=state)
    )
    assert moved["ok"] is True


def test_unknown_tool_name_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "delete_all_bookings", "{}"))
    assert result["ok"] is False


def test_malformed_arguments_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_business_info", "not json"))
    assert result["ok"] is False


def test_missing_required_arguments_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "create_booking", "{}"))
    assert result["ok"] is False


def test_voice_instructions_carry_the_business_local_date(garage):
    """#228: without today's date the model cannot turn "Saturday" into the
    YYYY-MM-DD get_available_slots needs. 23:30 UTC on 25 Sep is already
    Saturday 26 Sep in London (BST)."""
    from datetime import UTC, datetime

    from app.ai_voice.instructions import build_instructions

    garage.timezone = "Europe/London"
    text = build_instructions(garage, now=datetime(2026, 9, 25, 23, 30, tzinfo=UTC))
    assert "Saturday 26 September 2026 (2026-09-26)" in text
    assert "00:30" in text
    assert "Europe/London" in text
