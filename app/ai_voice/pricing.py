"""Infrastructure-cost calculation for one AI voice call - OpenAI Realtime
model usage and Twilio SIP trunk minutes.

This is CoMaz's own cost of running a call, never a retail/plan price
charged to a business - see the module docstring on
``app/models/communications/voice_call_metrics.py``. Nothing here bakes in
a markup; that belongs entirely to a future billing/pricing-plan layer that
reads these figures as one input, not to this module.

**What is actually measured vs. calculated, verified against the current
provider documentation (2026-09-23):**

- OpenAI's Realtime API reports token usage per response (input/output/
  cached audio tokens - see ``app/ai_voice/telemetry.py::record_usage``),
  but never hands back a billed dollar amount for a call. Every OpenAI cost
  figure this module produces is therefore CoMaz's own calculation off
  that measured token count, against a versioned rate table - always
  estimated, never a provider-reported charge, regardless of how precisely
  it's computed.
- A newer OpenAI voice model family (``gpt-live-1``, launched 2026-09-10)
  bills per second of session time ($0.05/min, i.e. per-second) rather than
  per audio token. This module supports both billing modes, selected by
  the rate table entry for the model actually used on the call.
- Twilio never reaches this backend with a per-call identifier for the
  OpenAI Realtime SIP path: the call's media (and its Twilio CallSid) goes
  straight to OpenAI over the SIP trunk, and nothing in
  ``app/ai_voice/`` ever sees or records that CallSid (see
  ``VoiceCallMetrics.external_call_id``'s own docstring). Twilio's Call
  resource *does* carry an authoritative ``price``/``price_unit`` once a
  call completes (Twilio's own docs: "Populated after the call is
  completed. May not be immediately available."), but without that CallSid
  there is nothing to fetch it by. Twilio cost here is therefore always a
  flat-rate calculation off this backend's own measured call duration, not
  a fetched Twilio charge - a real limitation, not an implementation
  shortcut, and it stays true until a CallSid-correlation path is built
  (out of scope here: it would need either a Twilio Voice Insights lookup
  by time window/destination or a webhook wired to the SIP trunk's own
  status callbacks, neither of which exists in this codebase today).

**Reconciliation:** ``reconcile_openai_cost``/``reconcile_twilio_cost``
exist so that if either provider ever does become able to supply an
authoritative post-call figure, that figure can overwrite the estimate
without touching the raw usage columns telemetry already preserved - nothing
calls them automatically today, because no such authoritative source exists
yet for either provider on this call path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------------
# Rate table
# --------------------------------------------------------------------------
#
# Each entry is a *version*, never edited in place once a call has been
# priced against it - see VoiceCallMetrics.openai_pricing_version's own
# comment. Adding a new rate means adding a new version key below, not
# changing an existing one; existing rows keep pointing at the version they
# were actually calculated under, so a rate change never silently rewrites
# history.
#
# Source for the figures below: https://developers.openai.com/api/docs/pricing
# (gpt-realtime family, per 1M audio tokens) and OpenAI's gpt-live-1 launch
# pricing ($0.05/min, billed per second), both verified 2026-09-23.

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class OpenAIRate:
    version: str
    mode: str  # "per_token" | "per_minute"
    # per_token mode - $ per 1,000,000 tokens:
    input_rate_per_million: Decimal | None = None
    cached_input_rate_per_million: Decimal | None = None
    output_rate_per_million: Decimal | None = None
    # per_minute mode - $ per second of session time:
    per_second_rate: Decimal | None = None


OPENAI_RATES: dict[str, OpenAIRate] = {
    "gpt-realtime-2.1": OpenAIRate(
        version="openai:gpt-realtime-2.1:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("32.00"),
        cached_input_rate_per_million=Decimal("0.40"),
        output_rate_per_million=Decimal("64.00"),
    ),
    "gpt-realtime-2": OpenAIRate(
        version="openai:gpt-realtime-2:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("32.00"),
        cached_input_rate_per_million=Decimal("0.40"),
        output_rate_per_million=Decimal("64.00"),
    ),
    "gpt-realtime-1.5": OpenAIRate(
        version="openai:gpt-realtime-1.5:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("32.00"),
        cached_input_rate_per_million=Decimal("0.40"),
        output_rate_per_million=Decimal("64.00"),
    ),
    "gpt-realtime": OpenAIRate(
        version="openai:gpt-realtime:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("32.00"),
        cached_input_rate_per_million=Decimal("0.40"),
        output_rate_per_million=Decimal("64.00"),
    ),
    "gpt-realtime-2.1-mini": OpenAIRate(
        version="openai:gpt-realtime-2.1-mini:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("10.00"),
        cached_input_rate_per_million=Decimal("0.30"),
        output_rate_per_million=Decimal("20.00"),
    ),
    "gpt-realtime-mini": OpenAIRate(
        version="openai:gpt-realtime-mini:2026-09",
        mode="per_token",
        input_rate_per_million=Decimal("10.00"),
        cached_input_rate_per_million=Decimal("0.30"),
        output_rate_per_million=Decimal("20.00"),
    ),
    "gpt-live-1": OpenAIRate(
        version="openai:gpt-live-1:2026-09",
        mode="per_minute",
        per_second_rate=(Decimal("0.05") / Decimal(60)),
    ),
}

# Twilio never reaches this backend with a per-call price for this path
# (see module docstring) - one flat, explicitly-estimated rate applies to
# every call's measured duration. US inbound-to-local-number origination,
# Twilio's own published US SIP Trunking rate, verified 2026-09-23:
# https://www.twilio.com/en-us/sip-trunking/pricing/us
TWILIO_INBOUND_RATE_VERSION = "twilio:sip_trunking_inbound_us_local:2026-09"
TWILIO_INBOUND_RATE_PER_MINUTE = Decimal("0.0034")


def _round_money(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_openai_cost(
    *,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None,
    duration_seconds: int | None,
) -> tuple[Decimal | None, str | None]:
    """``(amount, pricing_version)`` - both ``None`` when the model is
    unrecognised or there's nothing to price yet, never a guessed figure
    for a rate table CoMaz doesn't actually have."""
    if not model:
        return None, None
    rate = OPENAI_RATES.get(model)
    if rate is None:
        return None, None

    if rate.mode == "per_minute":
        if duration_seconds is None:
            return None, None
        assert rate.per_second_rate is not None
        amount = Decimal(duration_seconds) * rate.per_second_rate
        return _round_money(amount), rate.version

    # per_token
    if input_tokens is None and output_tokens is None and cached_input_tokens is None:
        return None, None
    cached = cached_input_tokens or 0
    uncached_input = max((input_tokens or 0) - cached, 0)
    assert rate.input_rate_per_million is not None
    assert rate.cached_input_rate_per_million is not None
    assert rate.output_rate_per_million is not None
    amount = (
        Decimal(uncached_input) * rate.input_rate_per_million / _MILLION
        + Decimal(cached) * rate.cached_input_rate_per_million / _MILLION
        + Decimal(output_tokens or 0) * rate.output_rate_per_million / _MILLION
    )
    return _round_money(amount), rate.version


def calculate_twilio_cost(*, duration_seconds: int | None) -> tuple[Decimal | None, str | None]:
    """Always estimated - see module docstring's Twilio limitation."""
    if duration_seconds is None:
        return None, None
    amount = Decimal(duration_seconds) / Decimal(60) * TWILIO_INBOUND_RATE_PER_MINUTE
    return _round_money(amount), TWILIO_INBOUND_RATE_VERSION


def reconcile_openai_cost(
    row, *, amount: Decimal, currency: str = "USD", source_version: str
) -> None:
    """Overwrite this row's OpenAI cost with a provider-reported figure,
    marking it no longer an estimate. Never touches the raw usage columns
    telemetry already captured - only the derived cost fields. Nothing in
    this codebase calls this automatically today (see module docstring);
    it exists for when/if OpenAI starts returning a billed figure this
    backend can fetch."""
    row.openai_cost_amount = amount
    row.openai_cost_currency = currency
    row.openai_cost_is_estimated = False
    row.openai_pricing_version = source_version


def reconcile_twilio_cost(
    row, *, amount: Decimal, currency: str = "USD", source_version: str
) -> None:
    """The Twilio analogue of :func:`reconcile_openai_cost` - for when a
    CallSid-correlation path exists to fetch Twilio's own authoritative
    ``price`` for the call (see module docstring)."""
    row.twilio_cost_amount = amount
    row.twilio_cost_currency = currency
    row.twilio_cost_is_estimated = False
    row.twilio_pricing_version = source_version
