"""app/ai_voice/call_controller.py - the Realtime call-control connection.

``run_call_controller`` uses the openai SDK's ``client.realtime.connect()``
directly, so these tests fake that connection (recv/send_raw) rather than
hitting a real Realtime WebSocket - the SDK's own transport is already
tested upstream; what's ours to test is the tool-dispatch/transfer/hangup
orchestration around it.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from app.ai_voice import call_controller
from app.ai_voice.call_controller import _extract_transfer_uri, run_call_controller
from app.models.booking_request import BookingRequest


def test_extract_transfer_uri_strips_the_internal_field():
    output = json.dumps({"ok": True, "callback_id": "c1", "_transfer_uri": "tel:+441234567890"})
    cleaned, transfer_uri = _extract_transfer_uri(output)
    assert transfer_uri == "tel:+441234567890"
    assert "_transfer_uri" not in json.loads(cleaned)
    assert json.loads(cleaned) == {"ok": True, "callback_id": "c1"}


def test_extract_transfer_uri_no_op_when_absent():
    output = json.dumps({"ok": True, "name": "Garage A"})
    cleaned, transfer_uri = _extract_transfer_uri(output)
    assert transfer_uri is None
    assert cleaned == output


def test_extract_transfer_uri_tolerates_malformed_json():
    cleaned, transfer_uri = _extract_transfer_uri("not json")
    assert transfer_uri is None
    assert cleaned == "not json"


class _FakeConnection:
    """Minimal stand-in for the SDK's RealtimeConnection: recv() replays a
    canned event sequence, send_raw() records what was sent."""

    def __init__(self, events):
        self._events = list(events)
        self.sent: list[dict] = []

    def recv(self):
        if not self._events:
            raise StopIteration("no more events")
        return self._events.pop(0)

    def send_raw(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_connection(monkeypatch, connection):
    fake_client = Mock()
    fake_client.realtime.connect.return_value = connection
    monkeypatch.setattr(call_controller, "OpenAI", Mock(return_value=fake_client))


def _next_voice_booking_day() -> str:
    day = datetime.now(UTC).date() + timedelta(days=3)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


def _booking_events(*, appointment_type_id: str, day: str, time: str, tool_suffix: str):
    """A complete model-tool sequence for one booking conversation.

    This deliberately drives the real controller and dispatcher rather than
    calling a tool in isolation: availability proof has to survive from the
    model's lookup event through the later booking event.
    """
    return [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id=f"availability_{tool_suffix}",
            name="get_available_slots",
            arguments=json.dumps({"appointment_type_id": appointment_type_id, "date": day}),
        ),
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id=f"booking_{tool_suffix}",
            name="create_booking",
            arguments=json.dumps(
                {
                    "appointment_type_id": appointment_type_id,
                    "date": day,
                    "time": time,
                    "first_name": "Alex",
                    "last_name": "Turner",
                    "vehicle_registration": f"VC{tool_suffix[-4:].upper()}",
                }
            ),
        ),
    ]


def test_run_call_controller_dispatches_a_tool_call_and_sends_the_result(monkeypatch, garage):
    function_call_event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="call_1",
        name="get_business_info",
        arguments="{}",
    )
    connection = _FakeConnection([function_call_event])
    _patch_connection(monkeypatch, connection)

    run_call_controller(
        api_key="sk-test", call_id="rtc_1", garage=garage, caller_phone="+447123456789"
    )

    assert len(connection.sent) == 2
    assert connection.sent[0]["type"] == "conversation.item.create"
    assert connection.sent[0]["item"]["type"] == "function_call_output"
    assert connection.sent[0]["item"]["call_id"] == "call_1"
    output = json.loads(connection.sent[0]["item"]["output"])
    assert output["ok"] is True
    assert output["name"] == garage.name
    assert connection.sent[1] == {"type": "response.create"}


def test_run_call_controller_hangs_up_after_handoff_with_no_fallback_number(monkeypatch, garage):
    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="call_1",
            name="request_human_handoff",
            arguments=json.dumps({"reason": "complex insurance question"}),
        ),
        SimpleNamespace(type="response.done"),
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)
    hangup_mock = Mock()
    refer_mock = Mock()
    monkeypatch.setattr(call_controller, "hangup_call", hangup_mock)
    monkeypatch.setattr(call_controller, "refer_call", refer_mock)

    run_call_controller(
        api_key="sk-test", call_id="rtc_2", garage=garage, caller_phone="+447123456789"
    )

    hangup_mock.assert_called_once_with("rtc_2")
    refer_mock.assert_not_called()
    # The transfer-only field must never reach OpenAI.
    output = json.loads(connection.sent[0]["item"]["output"])
    assert "_transfer_uri" not in output


def test_run_call_controller_transfers_when_a_fallback_number_is_configured(
    session, monkeypatch, garage
):
    from app.models.communications.garage_communication_settings import (
        GarageCommunicationSettings,
    )

    session.add(
        GarageCommunicationSettings(
            garage_id=garage.id,
            communications_enabled=True,
            voice_escalation_number="+441234567890",
        )
    )
    session.commit()
    session.refresh(garage)

    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="call_1",
            name="request_human_handoff",
            arguments=json.dumps({"reason": "wants a person"}),
        ),
        SimpleNamespace(type="response.done"),
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)
    hangup_mock = Mock()
    refer_mock = Mock()
    monkeypatch.setattr(call_controller, "hangup_call", hangup_mock)
    monkeypatch.setattr(call_controller, "refer_call", refer_mock)

    run_call_controller(
        api_key="sk-test", call_id="rtc_3", garage=garage, caller_phone="+447123456789"
    )

    refer_mock.assert_called_once_with("rtc_3", "tel:+441234567890")
    hangup_mock.assert_not_called()


def test_run_call_controller_exits_cleanly_when_the_connection_closes(monkeypatch, garage):
    connection = _FakeConnection([])  # recv() raises immediately
    _patch_connection(monkeypatch, connection)

    # Must not raise.
    run_call_controller(api_key="sk-test", call_id="rtc_4", garage=garage, caller_phone="")

    assert connection.sent == []


def test_repeated_function_event_replays_output_without_repeating_a_booking_action(
    monkeypatch, garage
):
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="tool_once",
        name="create_booking",
        arguments="{}",
    )
    connection = _FakeConnection([event, event])
    _patch_connection(monkeypatch, connection)
    dispatch = Mock(return_value=json.dumps({"ok": False, "error": "missing details"}))
    monkeypatch.setattr(call_controller, "dispatch_tool", dispatch)

    run_call_controller(api_key="sk-test", call_id="rtc_once", garage=garage, caller_phone="")

    dispatch.assert_called_once()
    assert len(connection.sent) == 4
    assert connection.sent[0]["item"]["output"] == connection.sent[2]["item"]["output"]


def test_failed_handoff_does_not_end_the_call(monkeypatch, garage):
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="handoff_failed",
        name="request_human_handoff",
        arguments="{}",
    )
    connection = _FakeConnection([event, SimpleNamespace(type="response.done")])
    _patch_connection(monkeypatch, connection)
    monkeypatch.setattr(
        call_controller,
        "dispatch_tool",
        Mock(return_value=json.dumps({"ok": False, "error": "unavailable"})),
    )
    hangup_mock = Mock()
    monkeypatch.setattr(call_controller, "hangup_call", hangup_mock)

    run_call_controller(
        api_key="sk-test", call_id="rtc_handoff_failed", garage=garage, caller_phone=""
    )

    hangup_mock.assert_not_called()


def test_booking_tool_logs_only_a_compact_authoritative_outcome(monkeypatch, garage, caplog):
    caplog.set_level(logging.INFO, logger="app.ai_voice.call_controller")
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="booking_result",
        name="create_booking",
        arguments="{}",
    )
    connection = _FakeConnection([event])
    _patch_connection(monkeypatch, connection)
    monkeypatch.setattr(
        call_controller,
        "dispatch_tool",
        Mock(
            return_value=json.dumps(
                {"ok": True, "status": "PENDING", "booking_reference": "BKSECRET"}
            )
        ),
    )

    run_call_controller(api_key="sk-test", call_id="rtc_observe", garage=garage, caller_phone="")

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "AI_VOICE_BOOKING_RESULT" in message and "outcome=PENDING" in message
        for message in messages
    )
    assert not any("BKSECRET" in message for message in messages)


def test_run_call_controller_never_raises_on_an_unexpected_crash(monkeypatch, garage):
    fake_client = Mock()
    fake_client.realtime.connect.side_effect = RuntimeError("network blip")
    monkeypatch.setattr(call_controller, "OpenAI", Mock(return_value=fake_client))

    # Must not raise - a crashed control connection is logged, not fatal.
    run_call_controller(api_key="sk-test", call_id="rtc_5", garage=garage, caller_phone="")


def test_controller_reconnect_with_different_openai_tool_ids_creates_one_request(
    monkeypatch, garage, garage_schedule, appointment_type
):
    """A provider replay may assign a fresh function id after reconnect.

    The durable provider call id, not only the ephemeral function id, must
    be the idempotency boundary for a single real phone call.
    """
    day = _next_voice_booking_day()
    first = _FakeConnection(
        _booking_events(
            appointment_type_id=str(appointment_type.id), day=day, time="09:00", tool_suffix="one"
        )
    )
    _patch_connection(monkeypatch, first)
    run_call_controller(
        api_key="sk-test", call_id="provider_call_once", garage=garage, caller_phone="+447123456789"
    )

    replay = _FakeConnection(
        _booking_events(
            appointment_type_id=str(appointment_type.id), day=day, time="09:00", tool_suffix="two"
        )
    )
    _patch_connection(monkeypatch, replay)
    run_call_controller(
        api_key="sk-test", call_id="provider_call_once", garage=garage, caller_phone="+447123456789"
    )

    requests = BookingRequest.query.filter_by(voice_call_id="provider_call_once").all()
    assert len(requests) == 1
    booking_outputs = [
        json.loads(event["item"]["output"])
        for event in replay.sent
        if event["type"] == "conversation.item.create" and event["item"]["call_id"] == "booking_two"
    ]
    assert booking_outputs == [
        {
            "ok": True,
            "booking_reference": requests[0].booking_reference,
            "status": "PENDING",
            "service": "MOT",
            "date": day,
            "time": "09:00",
        }
    ]


def test_separate_provider_call_can_create_its_own_legitimate_booking(
    monkeypatch, garage, garage_schedule, appointment_type
):
    day = _next_voice_booking_day()
    for provider_call, slot, suffix in (
        ("provider_call_first", "09:00", "first"),
        ("provider_call_second", "10:00", "second"),
    ):
        connection = _FakeConnection(
            _booking_events(
                appointment_type_id=str(appointment_type.id),
                day=day,
                time=slot,
                tool_suffix=suffix,
            )
        )
        _patch_connection(monkeypatch, connection)
        run_call_controller(
            api_key="sk-test", call_id=provider_call, garage=garage, caller_phone="+447123456789"
        )

    requests = BookingRequest.query.filter(
        BookingRequest.voice_call_id.in_(["provider_call_first", "provider_call_second"])
    ).all()
    assert {
        (request.voice_call_id, request.preferred_time.strftime("%H:%M")) for request in requests
    } == {
        ("provider_call_first", "09:00"),
        ("provider_call_second", "10:00"),
    }


def test_failed_booking_can_be_corrected_with_another_real_slot_in_the_same_call(
    monkeypatch, garage, garage_schedule, appointment_type
):
    day = _next_voice_booking_day()
    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="availability",
            name="get_available_slots",
            arguments=json.dumps({"appointment_type_id": str(appointment_type.id), "date": day}),
        ),
        # This time was not returned by the live lookup.  The rejection must
        # not poison the durable call id or prevent a later correction.
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="invalid_booking",
            name="create_booking",
            arguments=json.dumps(
                {
                    "appointment_type_id": str(appointment_type.id),
                    "date": day,
                    "time": "08:00",
                    "first_name": "Alex",
                    "last_name": "Turner",
                    "vehicle_registration": "VCFAIL",
                }
            ),
        ),
        *_booking_events(
            appointment_type_id=str(appointment_type.id), day=day, time="09:00", tool_suffix="fixed"
        )[1:],
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)
    run_call_controller(
        api_key="sk-test",
        call_id="provider_call_recover",
        garage=garage,
        caller_phone="+447123456789",
    )

    outputs = {
        event["item"]["call_id"]: json.loads(event["item"]["output"])
        for event in connection.sent
        if event["type"] == "conversation.item.create"
    }
    assert outputs["invalid_booking"]["ok"] is False
    assert outputs["booking_fixed"]["ok"] is True
    request = BookingRequest.query.filter_by(voice_call_id="provider_call_recover").one()
    assert request.preferred_time.strftime("%H:%M") == "09:00"
