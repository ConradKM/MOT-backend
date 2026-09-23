"""app/ai_voice/pricing.py - OpenAI/Twilio infrastructure cost calculation.

Every figure here is a CoMaz-side calculation off measured usage, never a
provider-billed actual (see the module's own docstring) - these tests
protect that distinction as much as the arithmetic.
"""

from decimal import Decimal

from app.ai_voice import pricing


def test_openai_per_token_cost_uses_uncached_and_cached_rates_separately():
    amount, version = pricing.calculate_openai_cost(
        model="gpt-realtime-2.1",
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        output_tokens=500_000,
        duration_seconds=120,
    )

    # 800,000 uncached input @ $32/M + 200,000 cached @ $0.40/M + 500,000 output @ $64/M
    expected = (
        Decimal(800_000) * Decimal("32.00") / Decimal(1_000_000)
        + Decimal(200_000) * Decimal("0.40") / Decimal(1_000_000)
        + Decimal(500_000) * Decimal("64.00") / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))
    assert amount == expected
    assert version == "openai:gpt-realtime-2.1:2026-09"


def test_openai_per_minute_model_bills_measured_duration_not_tokens():
    amount, version = pricing.calculate_openai_cost(
        model="gpt-live-1",
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        duration_seconds=90,
    )

    # $0.05/min == 90 seconds * ($0.05 / 60)
    expected = (Decimal(90) * Decimal("0.05") / Decimal(60)).quantize(Decimal("0.000001"))
    assert amount == expected
    assert version == "openai:gpt-live-1:2026-09"


def test_openai_cost_is_none_for_an_unrecognised_model():
    amount, version = pricing.calculate_openai_cost(
        model="some-future-model",
        input_tokens=100,
        cached_input_tokens=0,
        output_tokens=50,
        duration_seconds=60,
    )

    assert amount is None
    assert version is None


def test_openai_cost_is_none_without_a_model_or_usage():
    assert pricing.calculate_openai_cost(
        model=None,
        input_tokens=100,
        cached_input_tokens=0,
        output_tokens=50,
        duration_seconds=60,
    ) == (None, None)

    assert pricing.calculate_openai_cost(
        model="gpt-realtime-2.1",
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        duration_seconds=60,
    ) == (None, None)


def test_openai_per_minute_model_needs_duration():
    amount, version = pricing.calculate_openai_cost(
        model="gpt-live-1",
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        duration_seconds=None,
    )
    assert amount is None
    assert version is None


def test_twilio_cost_is_a_flat_rate_on_measured_duration():
    amount, version = pricing.calculate_twilio_cost(duration_seconds=180)

    expected = (Decimal(180) / Decimal(60) * pricing.TWILIO_INBOUND_RATE_PER_MINUTE).quantize(
        Decimal("0.000001")
    )
    assert amount == expected
    assert version == pricing.TWILIO_INBOUND_RATE_VERSION


def test_twilio_cost_is_none_without_a_duration():
    assert pricing.calculate_twilio_cost(duration_seconds=None) == (None, None)


def test_openai_cost_with_zero_tokens_is_a_real_zero_not_unknown():
    """Distinct from "no usage was ever recorded" (None) - here the call
    was priced, and genuinely cost nothing because nothing billable
    happened, which must show as $0.00, not "unavailable"."""
    amount, version = pricing.calculate_openai_cost(
        model="gpt-realtime-2.1",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        duration_seconds=5,
    )
    assert amount == Decimal("0.000000")
    assert version == "openai:gpt-realtime-2.1:2026-09"


def test_twilio_cost_with_zero_duration_is_a_real_zero():
    amount, version = pricing.calculate_twilio_cost(duration_seconds=0)
    assert amount == Decimal("0.000000")
    assert version == pricing.TWILIO_INBOUND_RATE_VERSION


def test_short_call_precision_is_not_rounded_to_zero():
    """A 3-second call at the Twilio inbound rate is well under a cent -
    6dp keeps it a real, summable number rather than noise."""
    amount, _version = pricing.calculate_twilio_cost(duration_seconds=3)
    assert amount is not None
    assert amount > Decimal(0)
    assert amount == Decimal(3) / Decimal(60) * pricing.TWILIO_INBOUND_RATE_PER_MINUTE


def test_openai_cost_calculation_is_deterministic_across_repeated_calls():
    """Same Decimal inputs must always produce the exact same Decimal
    output - no floating-point drift a caller could observe by retrying."""
    kwargs = {
        "model": "gpt-realtime-2.1",
        "input_tokens": 123_456,
        "cached_input_tokens": 7_890,
        "output_tokens": 45_678,
        "duration_seconds": 37,
    }
    first = pricing.calculate_openai_cost(**kwargs)
    second = pricing.calculate_openai_cost(**kwargs)
    assert first == second


def test_mini_model_uses_its_own_cheaper_rate_not_the_flagship_rate():
    flagship, _ = pricing.calculate_openai_cost(
        model="gpt-realtime-2.1",
        input_tokens=1_000_000,
        cached_input_tokens=0,
        output_tokens=0,
        duration_seconds=60,
    )
    mini, mini_version = pricing.calculate_openai_cost(
        model="gpt-realtime-2.1-mini",
        input_tokens=1_000_000,
        cached_input_tokens=0,
        output_tokens=0,
        duration_seconds=60,
    )
    assert mini < flagship
    assert mini_version == "openai:gpt-realtime-2.1-mini:2026-09"


def test_reconcile_openai_cost_overwrites_and_clears_the_estimate_flag():
    class _Row:
        openai_cost_amount = Decimal("1.2345")
        openai_cost_currency = "USD"
        openai_cost_is_estimated = True
        openai_pricing_version = "openai:gpt-realtime-2.1:2026-09"

    row = _Row()
    pricing.reconcile_openai_cost(
        row, amount=Decimal("0.9999"), source_version="openai:provider_reported:2026-10"
    )

    assert row.openai_cost_amount == Decimal("0.9999")
    assert row.openai_cost_is_estimated is False
    assert row.openai_pricing_version == "openai:provider_reported:2026-10"


def test_reconcile_twilio_cost_overwrites_and_clears_the_estimate_flag():
    class _Row:
        twilio_cost_amount = Decimal("0.0102")
        twilio_cost_currency = "USD"
        twilio_cost_is_estimated = True
        twilio_pricing_version = pricing.TWILIO_INBOUND_RATE_VERSION

    row = _Row()
    pricing.reconcile_twilio_cost(
        row, amount=Decimal("0.0088"), source_version="twilio:call_resource_price:2026-10"
    )

    assert row.twilio_cost_amount == Decimal("0.0088")
    assert row.twilio_cost_is_estimated is False
    assert row.twilio_pricing_version == "twilio:call_resource_price:2026-10"
