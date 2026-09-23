"""Voice call usage/cost/quality telemetry aggregation
(app/platform_admin/voice_telemetry.py), exposed at
GET .../tenants/<id>/voice-telemetry and GET .../stats/voice-telemetry.

Same rule as the rest of Platform Admin stats: a rate over an empty
denominator is null, never 0.
"""

import datetime
from decimal import Decimal

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


def test_cost_totals_are_summed_and_flagged_estimated(platform_client, garage, session):
    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal("0.1234"),
        openai_cost_currency="USD",
        openai_cost_is_estimated=True,
        twilio_cost_amount=Decimal("0.0034"),
        twilio_cost_currency="USD",
        twilio_cost_is_estimated=True,
    )
    _call(
        session,
        garage,
        external_call_id="c2",
        openai_cost_amount=Decimal("0.2000"),
        openai_cost_currency="USD",
        openai_cost_is_estimated=True,
        twilio_cost_amount=Decimal("0.0068"),
        twilio_cost_currency="USD",
        twilio_cost_is_estimated=True,
    )
    # A call the platform has no rate for at all - excluded from the cost
    # total, not treated as a $0 contribution.
    _call(session, garage, external_call_id="c3")

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert Decimal(body["cost"]["openai_total"]) == Decimal("0.3234")
    assert body["cost"]["openai_is_estimated"] is True
    assert Decimal(body["cost"]["twilio_total"]) == Decimal("0.0102")
    assert body["cost"]["twilio_is_estimated"] is True


def test_twilio_cost_is_still_totalled_when_openai_cost_is_missing(
    platform_client, garage, session
):
    """A call under a model CoMaz has no OpenAI rate for still has a
    Twilio figure - the two totals must not share one restrictive filter."""
    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=None,
        twilio_cost_amount=Decimal("0.0034"),
        twilio_cost_currency="USD",
        twilio_cost_is_estimated=True,
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert body["cost"]["openai_total"] is None
    assert Decimal(body["cost"]["twilio_total"]) == Decimal("0.0034")


def test_platform_wide_endpoint_aggregates_across_businesses(
    platform_client, garage, second_garage, session
):
    _call(session, garage, external_call_id="c1")
    _call(session, second_garage, external_call_id="c2")

    body = platform_client.get("/api/platform-admin/stats/voice-telemetry").json

    assert body["call_count"] == 2
    assert body["garage_id"] is None


def test_priced_call_counts_distinguish_zero_cost_from_no_priced_calls(
    platform_client, garage, session
):
    """ "£0 cost" and "cost unavailable" must be distinguishable - a business
    with no priced calls at all must not look identical to one whose calls
    happened to cost nothing."""
    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json
    assert body["cost"]["openai_total"] is None
    assert body["cost"]["openai_priced_calls"] == 0

    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal(0),
        openai_cost_currency="USD",
        openai_cost_is_estimated=True,
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json
    assert Decimal(body["cost"]["openai_total"]) == Decimal(0)
    assert body["cost"]["openai_priced_calls"] == 1


def test_combined_total_only_appears_when_both_providers_priced_in_the_same_currency(
    platform_client, garage, session
):
    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal("0.30"),
        openai_cost_currency="USD",
        twilio_cost_amount=Decimal("0.01"),
        twilio_cost_currency="USD",
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    assert Decimal(body["cost"]["combined_total"]) == Decimal("0.31")
    assert body["cost"]["combined_currency"] == "USD"


def test_combined_total_is_absent_when_only_one_provider_has_priced_calls(
    platform_client, garage, session
):
    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal("0.30"),
        openai_cost_currency="USD",
        twilio_cost_amount=None,
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    # Never invent a combined figure out of one real component and one
    # unknown - report the components separately instead.
    assert body["cost"]["combined_total"] is None
    assert body["cost"]["combined_currency"] is None


def test_combined_total_is_absent_when_currencies_do_not_match(platform_client, garage, session):
    _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal("0.30"),
        openai_cost_currency="USD",
        twilio_cost_amount=Decimal("0.01"),
        twilio_cost_currency="GBP",
    )

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry").json

    # Never add USD to GBP - report the components separately instead.
    assert body["cost"]["combined_total"] is None
    assert body["cost"]["combined_currency"] is None


def test_pricing_version_is_preserved_per_call_not_recomputed_by_the_report(
    platform_client, garage, session
):
    """A stored row keeps whichever pricing version actually priced it -
    the reporting layer reads that figure back, it never recalculates
    under today's rate table."""
    row = _call(
        session,
        garage,
        external_call_id="c1",
        openai_cost_amount=Decimal("1.00"),
        openai_cost_currency="USD",
        openai_pricing_version="openai:gpt-realtime-2.1:2025-01",
    )

    # A later rate change would add a new version, never edit this one -
    # confirm the stored row is untouched by the report simply existing.
    platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry")

    session.refresh(row)
    assert row.openai_pricing_version == "openai:gpt-realtime-2.1:2025-01"


def test_voice_telemetry_schema_serializes_every_cost_field(platform_client, garage, session):
    """A regression guard on the response shape itself - every field the
    schema declares must actually round-trip through the API, not just
    exist in the aggregation function's return dict."""
    _call(
        session,
        garage,
        external_call_id="c1",
        duration_seconds=42,
        openai_cost_amount=Decimal("0.123456"),
        openai_cost_currency="USD",
        openai_cost_is_estimated=True,
        openai_pricing_version="openai:gpt-realtime-2.1:2026-09",
        twilio_cost_amount=Decimal("0.000200"),
        twilio_cost_currency="USD",
        twilio_cost_is_estimated=True,
        twilio_pricing_version="twilio:sip_trunking_inbound_us_local:2026-09",
    )

    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/voice-telemetry")

    assert response.status_code == 200
    cost = response.json["cost"]
    for field in (
        "openai_total",
        "openai_is_estimated",
        "openai_priced_calls",
        "twilio_total",
        "twilio_is_estimated",
        "twilio_priced_calls",
        "combined_total",
        "combined_currency",
    ):
        assert field in cost
    assert Decimal(cost["openai_total"]) == Decimal("0.123456")
    assert Decimal(cost["twilio_total"]) == Decimal("0.000200")
