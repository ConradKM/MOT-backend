"""app/ai_voice/telemetry.py - usage/cost/quality capture for one AI voice
call, and its wiring into app/ai_voice/call_controller.py.

Every write is best-effort (a telemetry failure must never break or end a
live call) - these tests exercise the happy path plus "no row yet" as a
silent no-op, not a raised exception.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.ai_voice import call_controller, telemetry
from app.ai_voice.call_controller import run_call_controller
from app.models.communications.voice_call_metrics import (
    END_REASON_CONNECTION_CLOSED,
    END_REASON_HANDOFF,
    END_REASON_HANGUP,
    VoiceCallMetrics,
)


def test_start_call_creates_one_row(session, garage):
    telemetry.start_call(garage.id, "call_1")

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.garage_id == garage.id
    assert row.started_at is not None
    assert row.ended_at is None


def test_start_call_is_idempotent(session, garage):
    telemetry.start_call(garage.id, "call_1")
    telemetry.start_call(garage.id, "call_1")

    assert VoiceCallMetrics.query.filter_by(external_call_id="call_1").count() == 1


def test_recording_functions_are_silent_no_ops_without_a_row(session, garage):
    # No start_call happened for this id - must not raise.
    telemetry.record_tool_call("missing", tool="t", outcome="ok", latency_ms=1)
    telemetry.record_booking_outcome("missing", outcome="PENDING")
    telemetry.record_escalation("missing")
    telemetry.record_usage("missing", input_tokens=10)
    telemetry.finish_call("missing", end_reason=END_REASON_HANGUP)

    assert VoiceCallMetrics.query.count() == 0


def test_record_tool_call_appends_and_counts(session, garage):
    telemetry.start_call(garage.id, "call_1")

    telemetry.record_tool_call(
        "call_1", tool="get_business_info", outcome="succeeded", latency_ms=50
    )
    telemetry.record_tool_call("call_1", tool="create_booking", outcome="PENDING", latency_ms=120)

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.tool_call_count == 2
    assert row.tool_calls == [
        {"tool": "get_business_info", "outcome": "succeeded", "latency_ms": 50},
        {"tool": "create_booking", "outcome": "PENDING", "latency_ms": 120},
    ]


def test_record_booking_outcome_sets_a_short_label(session, garage):
    telemetry.start_call(garage.id, "call_1")

    telemetry.record_booking_outcome("call_1", outcome="PENDING")

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.booking_outcome == "PENDING"


def test_record_escalation_sets_the_flag(session, garage):
    telemetry.start_call(garage.id, "call_1")

    telemetry.record_escalation("call_1")

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.escalated_to_human is True


def test_record_usage_accumulates_across_calls(session, garage):
    telemetry.start_call(garage.id, "call_1")

    telemetry.record_usage("call_1", input_tokens=100, output_tokens=40)
    telemetry.record_usage("call_1", input_tokens=20, cached_input_tokens=5)

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.input_tokens == 120
    assert row.output_tokens == 40
    assert row.cached_input_tokens == 5


def test_finish_call_sets_measured_duration_and_reason(session, garage):
    telemetry.start_call(garage.id, "call_1")

    telemetry.finish_call("call_1", end_reason=END_REASON_HANGUP)

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.ended_at is not None
    assert row.end_reason == END_REASON_HANGUP
    assert row.duration_seconds is not None
    assert row.duration_seconds >= 0


def test_finish_call_calculates_openai_and_twilio_cost_from_measured_usage(session, garage):
    telemetry.start_call(garage.id, "call_1")
    telemetry.record_usage("call_1", input_tokens=1000, output_tokens=500, cached_input_tokens=100)

    telemetry.finish_call("call_1", end_reason=END_REASON_HANGUP)

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    # OPENAI_REALTIME_MODEL defaults to gpt-realtime-2.1 in TestConfig.
    assert row.openai_cost_amount is not None
    assert row.openai_cost_currency == "USD"
    assert row.openai_cost_is_estimated is True
    assert row.openai_pricing_version == "openai:gpt-realtime-2.1:2026-09"
    assert row.twilio_cost_amount is not None
    assert row.twilio_cost_is_estimated is True
    assert row.twilio_pricing_version is not None


def test_finish_call_leaves_cost_null_for_an_unrecognised_model(session, garage, app):
    app.config["OPENAI_REALTIME_MODEL"] = "some-future-model"
    telemetry.start_call(garage.id, "call_1")
    telemetry.record_usage("call_1", input_tokens=1000, output_tokens=500)

    telemetry.finish_call("call_1", end_reason=END_REASON_HANGUP)

    row = VoiceCallMetrics.query.filter_by(external_call_id="call_1").one()
    assert row.openai_cost_amount is None
    assert row.openai_pricing_version is None
    # Twilio's flat-rate calculation is independent of the OpenAI model.
    assert row.twilio_cost_amount is not None


# --------------------------------------------------------------------------
# Wired into the live call-control loop
# --------------------------------------------------------------------------


class _FakeConnection:
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


def test_controller_records_tool_calls_and_finishes_on_connection_close(
    session, monkeypatch, garage
):
    telemetry.start_call(garage.id, "rtc_telemetry_1")
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="c1",
        name="get_business_info",
        arguments="{}",
    )
    connection = _FakeConnection([event])
    _patch_connection(monkeypatch, connection)

    run_call_controller(
        api_key="sk-test", call_id="rtc_telemetry_1", garage=garage, caller_phone=""
    )

    row = VoiceCallMetrics.query.filter_by(external_call_id="rtc_telemetry_1").one()
    assert row.tool_call_count == 1
    assert row.tool_calls[0]["tool"] == "get_business_info"
    assert row.end_reason == END_REASON_CONNECTION_CLOSED
    assert row.ended_at is not None


def test_controller_records_booking_outcome(session, monkeypatch, garage):
    telemetry.start_call(garage.id, "rtc_telemetry_2")
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="c1",
        name="create_booking",
        arguments="{}",
    )
    connection = _FakeConnection([event])
    _patch_connection(monkeypatch, connection)
    monkeypatch.setattr(
        call_controller,
        "dispatch_tool",
        Mock(return_value=json.dumps({"ok": True, "status": "PENDING"})),
    )

    run_call_controller(
        api_key="sk-test", call_id="rtc_telemetry_2", garage=garage, caller_phone=""
    )

    row = VoiceCallMetrics.query.filter_by(external_call_id="rtc_telemetry_2").one()
    assert row.booking_outcome == "PENDING"


def test_controller_records_escalation_and_hangup_end_reason(session, monkeypatch, garage):
    telemetry.start_call(garage.id, "rtc_telemetry_3")
    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="c1",
            name="request_human_handoff",
            arguments=json.dumps({"reason": "wants a person"}),
        ),
        SimpleNamespace(type="response.done"),
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)
    monkeypatch.setattr(call_controller, "hangup_call", Mock())
    monkeypatch.setattr(call_controller, "refer_call", Mock())

    run_call_controller(
        api_key="sk-test", call_id="rtc_telemetry_3", garage=garage, caller_phone=""
    )

    row = VoiceCallMetrics.query.filter_by(external_call_id="rtc_telemetry_3").one()
    assert row.escalated_to_human is True
    assert row.end_reason == END_REASON_HANGUP


def test_controller_records_handoff_end_reason_when_transferred(session, monkeypatch, garage):
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

    telemetry.start_call(garage.id, "rtc_telemetry_4")
    events = [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="c1",
            name="request_human_handoff",
            arguments=json.dumps({"reason": "wants a person"}),
        ),
        SimpleNamespace(type="response.done"),
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)
    monkeypatch.setattr(call_controller, "hangup_call", Mock())
    monkeypatch.setattr(call_controller, "refer_call", Mock())

    run_call_controller(
        api_key="sk-test", call_id="rtc_telemetry_4", garage=garage, caller_phone=""
    )

    row = VoiceCallMetrics.query.filter_by(external_call_id="rtc_telemetry_4").one()
    assert row.end_reason == END_REASON_HANDOFF


def test_controller_records_usage_from_response_done(session, monkeypatch, garage):
    telemetry.start_call(garage.id, "rtc_telemetry_5")
    events = [
        SimpleNamespace(
            type="response.done",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=150,
                    output_tokens=60,
                    input_token_details=SimpleNamespace(cached_tokens=30),
                )
            ),
        ),
    ]
    connection = _FakeConnection(events)
    _patch_connection(monkeypatch, connection)

    run_call_controller(
        api_key="sk-test", call_id="rtc_telemetry_5", garage=garage, caller_phone=""
    )

    row = VoiceCallMetrics.query.filter_by(external_call_id="rtc_telemetry_5").one()
    assert row.input_tokens == 150
    assert row.output_tokens == 60
    assert row.cached_input_tokens == 30


def test_controller_never_raises_when_no_telemetry_row_exists(monkeypatch, garage):
    """A call that somehow reached the controller without a telemetry row
    (e.g. app/ai_voice/routes.py's own telemetry write failed) must still
    run the call normally - telemetry is never load-bearing."""
    event = SimpleNamespace(
        type="response.function_call_arguments.done",
        call_id="c1",
        name="get_business_info",
        arguments="{}",
    )
    connection = _FakeConnection([event])
    _patch_connection(monkeypatch, connection)

    run_call_controller(
        api_key="sk-test", call_id="rtc_no_telemetry_row", garage=garage, caller_phone=""
    )

    assert connection.sent[1] == {"type": "response.create"}
