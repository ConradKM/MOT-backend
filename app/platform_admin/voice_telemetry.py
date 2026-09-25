"""Read-only aggregation over ``voice_call_metrics`` - usage, cost and
quality telemetry for AI-handled voice calls, in the same style as
``app/platform_admin/stats.py`` (aggregate in SQL, rates are ``None`` over
an empty denominator, never ``0``).

Cost totals are reported only from rows that actually carry a cost figure -
a business with no calls, or calls under a model CoMaz has no rate for,
report ``None`` rather than a false zero. Every reported total is
estimated (see app/ai_voice/pricing.py for exactly why neither OpenAI nor
Twilio hands this backend a provider-billed figure for this call path
today); ``openai_is_estimated``/``twilio_is_estimated`` stay in the
response so a report can never silently start presenting an estimate as an
actual once reconciliation (pricing.py's reconcile_* functions) is wired
up for either provider.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select

from app.extensions import db
from app.models.communications.voice_call_metrics import VoiceCallMetrics

from .stats import DEFAULT_PERIOD_DAYS, clamp_days


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(100 * numerator / denominator, 1)


def _grouped_nonnull(column, *filters) -> dict:
    query = select(column, func.count()).where(column.is_not(None), *filters)
    rows = db.session.execute(query.group_by(column)).all()
    return {key: count for key, count in rows}


def voice_telemetry_stats(
    garage_id: uuid.UUID | None, *, days: int = DEFAULT_PERIOD_DAYS, now: datetime | None = None
) -> dict:
    """Everything a voice-usage/cost/quality report needs. ``garage_id=None``
    aggregates across every business."""
    now = now or _utcnow()
    days = clamp_days(days)
    since = now - timedelta(days=days)

    filters = [VoiceCallMetrics.started_at >= since]
    if garage_id is not None:
        filters.append(VoiceCallMetrics.garage_id == garage_id)

    totals = db.session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(VoiceCallMetrics.duration_seconds), 0),
            func.avg(VoiceCallMetrics.duration_seconds),
            func.coalesce(func.sum(VoiceCallMetrics.input_tokens), 0),
            func.coalesce(func.sum(VoiceCallMetrics.output_tokens), 0),
            func.coalesce(func.sum(VoiceCallMetrics.cached_input_tokens), 0),
            func.coalesce(func.sum(VoiceCallMetrics.tool_call_count), 0),
            func.count().filter(VoiceCallMetrics.escalated_to_human.is_(True)),
            func.count().filter(VoiceCallMetrics.ended_at.is_(None)),
        ).where(*filters)
    ).one()
    (
        call_count,
        total_duration_seconds,
        avg_duration_seconds,
        total_input_tokens,
        total_output_tokens,
        total_cached_input_tokens,
        total_tool_calls,
        escalated_count,
        still_open_count,
    ) = cast(
        "tuple[int, int, float | None, int, int, int, int, int, int]",
        totals,
    )

    # Two independent queries, not one shared WHERE: a call under a model
    # CoMaz has no OpenAI rate for still gets a Twilio figure (and vice
    # versa in principle), so restricting both sums to "openai cost is not
    # null" would silently drop a real Twilio contribution.
    (
        openai_cost_total,
        openai_cost_all_estimated,
        openai_priced_calls,
        openai_currencies,
    ) = cast(
        "tuple[float | None, bool | None, int, int]",
        db.session.execute(
            select(
                func.sum(VoiceCallMetrics.openai_cost_amount),
                func.bool_and(VoiceCallMetrics.openai_cost_is_estimated),
                func.count(),
                func.count(func.distinct(VoiceCallMetrics.openai_cost_currency)),
            ).where(*filters, VoiceCallMetrics.openai_cost_amount.is_not(None))
        ).one(),
    )
    (
        twilio_cost_total,
        twilio_cost_all_estimated,
        twilio_priced_calls,
        twilio_currencies,
    ) = cast(
        "tuple[float | None, bool | None, int, int]",
        db.session.execute(
            select(
                func.sum(VoiceCallMetrics.twilio_cost_amount),
                func.bool_and(VoiceCallMetrics.twilio_cost_is_estimated),
                func.count(),
                func.count(func.distinct(VoiceCallMetrics.twilio_cost_currency)),
            ).where(*filters, VoiceCallMetrics.twilio_cost_amount.is_not(None))
        ).one(),
    )

    # A combined figure is only meaningful when both providers priced at
    # least one call and each did so in exactly one currency - "report
    # components separately instead of lying with arithmetic" the moment
    # either condition breaks (a mixed-currency period, or one provider
    # with nothing priced yet).
    combined_total = None
    combined_currency = None
    if (
        openai_cost_total is not None
        and twilio_cost_total is not None
        and openai_currencies == 1
        and twilio_currencies == 1
    ):
        openai_currency = db.session.execute(
            select(VoiceCallMetrics.openai_cost_currency)
            .where(*filters, VoiceCallMetrics.openai_cost_amount.is_not(None))
            .limit(1)
        ).scalar()
        twilio_currency = db.session.execute(
            select(VoiceCallMetrics.twilio_cost_currency)
            .where(*filters, VoiceCallMetrics.twilio_cost_amount.is_not(None))
            .limit(1)
        ).scalar()
        if openai_currency == twilio_currency:
            combined_total = openai_cost_total + twilio_cost_total
            combined_currency = openai_currency

    booking_outcomes = _grouped_nonnull(VoiceCallMetrics.booking_outcome, *filters)
    end_reasons = _grouped_nonnull(VoiceCallMetrics.end_reason, *filters)

    return {
        "garage_id": garage_id,
        "period_days": days,
        "period_start": since,
        "period_end": now,
        "call_count": call_count,
        "total_duration_seconds": int(total_duration_seconds),
        "avg_duration_seconds": (
            round(float(avg_duration_seconds), 1) if avg_duration_seconds is not None else None
        ),
        "usage": {
            "total_input_tokens": int(total_input_tokens),
            "total_output_tokens": int(total_output_tokens),
            "total_cached_input_tokens": int(total_cached_input_tokens),
        },
        "cost": {
            "openai_total": openai_cost_total,
            "openai_is_estimated": bool(openai_cost_all_estimated)
            if openai_cost_total is not None
            else None,
            "openai_priced_calls": openai_priced_calls,
            "twilio_total": twilio_cost_total,
            "twilio_is_estimated": bool(twilio_cost_all_estimated)
            if twilio_cost_total is not None
            else None,
            "twilio_priced_calls": twilio_priced_calls,
            "combined_total": combined_total,
            "combined_currency": combined_currency,
        },
        "tool_calls": {
            "total": int(total_tool_calls),
            "avg_per_call": (round(total_tool_calls / call_count, 1) if call_count else None),
        },
        "booking_outcomes": booking_outcomes,
        "end_reasons": end_reasons,
        "escalated_count": escalated_count,
        "escalation_rate": _rate(escalated_count, call_count),
        "still_open_count": still_open_count,
    }
