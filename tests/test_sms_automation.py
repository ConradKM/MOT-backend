"""SMS notification wiring - app/communications/sms_automation.py.

Every handler here is exercised via emit_event, with send_sms_message itself
mocked out (its own provider/skip/failure behaviour is covered by
tests/test_communications_providers.py) - these tests only check that the
SMS_NOTIFICATIONS_ENABLED gate and the right booking-event -> message
wiring are correct.
"""

from unittest.mock import Mock

import pytest

from app.communications import events as comms_events
from app.communications import sms_automation


@pytest.fixture(autouse=True)
def _reset_event_handlers():
    comms_events._reset_handlers_for_tests()
    yield
    comms_events._reset_handlers_for_tests()


@pytest.fixture()
def sms_mock(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(sms_automation, "send_sms_message", mock)
    return mock


def test_no_sms_sent_when_notifications_disabled(app, garage, sms_mock):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = False
    sms_automation.register_sms_handlers()

    booking_request = type(
        "BR",
        (),
        {
            "customer_first_name": "Alex",
            "customer_phone": "+447123456789",
            "booking_reference": "BK1",
        },
    )()
    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request
    )

    sms_mock.assert_not_called()


def test_booking_request_created_sends_sms_when_enabled(app, garage, sms_mock):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms_automation.register_sms_handlers()

    booking_request = type(
        "BR",
        (),
        {
            "customer_first_name": "Alex",
            "customer_phone": "+447123456789",
            "booking_reference": "BK1",
        },
    )()
    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request
    )

    sms_mock.assert_called_once()
    kwargs = sms_mock.call_args.kwargs
    assert kwargs["to"] == "+447123456789"
    assert "BK1" in kwargs["body"]


def test_booking_request_created_skips_without_a_phone_number(app, garage, sms_mock):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms_automation.register_sms_handlers()

    booking_request = type(
        "BR",
        (),
        {"customer_first_name": "Alex", "customer_phone": None, "booking_reference": "BK1"},
    )()
    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request
    )

    sms_mock.assert_not_called()


def test_appointment_cancelled_sends_sms_to_the_customer(app, garage, sms_mock):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms_automation.register_sms_handlers()

    customer = type("Customer", (), {"phone": "+447123456789"})()
    appointment = type(
        "Appointment",
        (),
        {"customer": customer, "start_time": __import__("datetime").datetime(2026, 1, 1, 9, 0)},
    )()
    comms_events.emit_event(
        comms_events.APPOINTMENT_CANCELLED, garage=garage, appointment=appointment
    )

    sms_mock.assert_called_once()
    assert sms_mock.call_args.kwargs["to"] == "+447123456789"


def test_register_sms_handlers_is_idempotent(app, garage, sms_mock):
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    sms_automation.register_sms_handlers()
    sms_automation.register_sms_handlers()

    booking_request = type(
        "BR",
        (),
        {
            "customer_first_name": "Alex",
            "customer_phone": "+447123456789",
            "booking_reference": "BK1",
        },
    )()
    comms_events.emit_event(
        comms_events.BOOKING_REQUEST_CREATED, garage=garage, booking_request=booking_request
    )

    sms_mock.assert_called_once()
