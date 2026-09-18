"""app/ai_voice/tools.py - the tool schemas and tenant-scoped dispatcher the
OpenAI Realtime model calls during a phone call.
"""

import json
from datetime import UTC, datetime, timedelta

from app.ai_voice.tools import TOOL_SCHEMAS, dispatch_tool
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


def test_cancel_appointment_cancels_the_callers_own_appointment(garage, customer, make_appointment):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appt = make_appointment(start)
    args = json.dumps({"appointment_id": str(appt.id)})
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
    args = json.dumps({"appointment_id": str(appt.id)})
    result = json.loads(dispatch_tool(garage, other.phone, "cancel_appointment", args))
    assert result["ok"] is False
    assert appt.status == "BOOKED"


def test_reschedule_appointment_moves_the_callers_own_appointment(
    garage, garage_schedule, customer, make_appointment
):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    appt = make_appointment(start)
    new_day = _future_weekday(days_ahead=10)
    args = json.dumps(
        {"appointment_id": str(appt.id), "date": new_day.isoformat(), "time": "10:00"}
    )
    result = json.loads(dispatch_tool(garage, customer.phone, "reschedule_appointment", args))
    assert result["ok"] is True
    assert appt.start_time.date() == new_day
    assert appt.start_time.strftime("%H:%M") == "10:00"


def test_unknown_tool_name_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "delete_all_bookings", "{}"))
    assert result["ok"] is False


def test_malformed_arguments_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "get_business_info", "not json"))
    assert result["ok"] is False


def test_missing_required_arguments_is_a_clean_error(garage):
    result = json.loads(dispatch_tool(garage, "+447123456789", "create_booking", "{}"))
    assert result["ok"] is False
