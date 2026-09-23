"""Voice call usage/cost/quality telemetry aggregation
(app/platform_admin/voice_telemetry.py), exposed at
GET .../tenants/<id>/voice-telemetry and GET .../stats/voice-telemetry.

Same rule as the rest of Platform Admin stats: a rate over an empty
denominator is null, never 0.
"""

import datetime

from app.models.communications.voice_call_metrics import (
    END_REASON_CONNECTION_CLOSED,
    END_REASON_HANDOFF,
    VoiceCallMetrics,
)


def _call(session, garage, *, external_call_id, **kwargs):
    row = VoiceCallMetrics(
        garage_id=garage.id,
        external_call_id=external_call_id,
        started_at=datetime.datetime.now(datetime.UTC),
        **kwargs,
    )
    session.add(row)
    session.commit()
    return row


def test_stats_for_a_business_with_no_calls_are_zero_not_null(platform_client, garage):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry")

    assert response.status_code == 200
    body = response.json
    assert body["call_count"] == 0
    assert body["total_duration_seconds"] == 0
    assert body["avg_duration_seconds"] is None
    assert body["escalation_rate"] is None
    assert body["cost"]["openai_total"] is None


def test_counts_calls_duration_tokens_and_tool_calls(platform_client, garage, session):
    _call(
        session,
        garage,
        external_call_id="c1",
        duration_seconds=120,
        input_tokens=100,
        output_tokens=40,
        tool_call_count=2,
    )
    _call(
        session,
        garage,
        external_call_id="c2",
        duration_seconds=60,
        input_tokens=50,
        output_tokens=20,
        tool_call_count=1,
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert body["call_count"] == 2
    assert body["total_duration_seconds"] == 180
    assert body["avg_duration_seconds"] == 90.0
    assert body["usage"]["total_input_tokens"] == 150
    assert body["usage"]["total_output_tokens"] == 60
    assert body["tool_calls"]["total"] == 3
    assert body["tool_calls"]["avg_per_call"] == 1.5


def test_escalation_rate(platform_client, garage, session):
    _call(session, garage, external_call_id="c1", escalated_to_human=True)
    _call(session, garage, external_call_id="c2", escalated_to_human=True)
    _call(session, garage, external_call_id="c3", escalated_to_human=False)
    _call(session, garage, external_call_id="c4", escalated_to_human=False)

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert body["escalated_count"] == 2
    assert body["escalation_rate"] == 50.0


def test_booking_outcomes_and_end_reasons_are_grouped(platform_client, garage, session):
    _call(session, garage, external_call_id="c1", booking_outcome="PENDING")
    _call(session, garage, external_call_id="c2", booking_outcome="PENDING")
    _call(session, garage, external_call_id="c3", booking_outcome="failed")
    _call(session, garage, external_call_id="c4", end_reason=END_REASON_HANDOFF)
    _call(session, garage, external_call_id="c5", end_reason=END_REASON_CONNECTION_CLOSED)

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert body["booking_outcomes"] == {"PENDING": 2, "failed": 1}
    assert body["end_reasons"] == {END_REASON_HANDOFF: 1, END_REASON_CONNECTION_CLOSED: 1}


def test_a_call_outside_the_period_is_excluded(platform_client, garage, session):
    old = _call(session, garage, external_call_id="c_old")
    old.started_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=400)
    session.commit()
    _call(session, garage, external_call_id="c_recent")

    body = platform_client.get(
        f"/api/platform-admin/tenants/{garage.id}/voice-telemetry?days=30"
    ).json

    assert body["call_count"] == 1


def test_a_call_from_another_business_is_excluded(platform_client, garage, second_garage, session):
    _call(session, garage, external_call_id="c_mine")
    _call(session, second_garage, external_call_id="c_theirs")

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert body["call_count"] == 1


def test_platform_wide_endpoint_aggregates_across_businesses(
    platform_client, garage, second_garage, session
):
    _call(session, garage, external_call_id="c1")
    _call(session, second_garage, external_call_id="c2")

    body = platform_client.get("/api/platform-admin/stats/voice-telemetry").json

    assert body["call_count"] == 2
    assert body["garage_id"] is None
