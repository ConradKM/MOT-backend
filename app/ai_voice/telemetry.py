"""Usage/cost/quality telemetry capture for one AI voice call.

Every write here is best-effort: a telemetry failure must never break or
end a live call. Callers (routes.py, call_controller.py) call these
functions and let them swallow their own DB errors rather than wrapping
every call site in its own try/except.

No transcript or free-text customer content is ever passed into this
module - only counts, short outcome labels, and timings. See
app/models/communications/voice_call_metrics.py's module docstring for the
measured-vs-estimated cost convention.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.models.communications.voice_call_metrics import (
    PROVIDER_OPENAI_REALTIME_SIP,
    VoiceCallMetrics,
)

from . import pricing

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def start_call(garage_id, external_call_id: str) -> None:
    """One row per call, created once it's accepted - mirrors the
    CommunicationLog row created alongside it in app/ai_voice/routes.py."""
    try:
        if db.session.query(VoiceCallMetrics).filter_by(external_call_id=external_call_id).first():
            return
        db.session.add(
            VoiceCallMetrics(
                garage_id=garage_id,
                provider=PROVIDER_OPENAI_REALTIME_SIP,
                external_call_id=external_call_id,
                started_at=_now(),
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_START_FAILED callSid=%s", external_call_id)


def _get(external_call_id: str) -> VoiceCallMetrics | None:
    return db.session.query(VoiceCallMetrics).filter_by(external_call_id=external_call_id).first()


def record_tool_call(external_call_id: str, *, tool: str, outcome: str, latency_ms: int) -> None:
    try:
        row = _get(external_call_id)
        if row is None:
            return
        calls = list(row.tool_calls or [])
        calls.append({"tool": tool, "outcome": outcome, "latency_ms": latency_ms})
        row.tool_calls = calls
        row.tool_call_count = len(calls)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_TOOL_CALL_FAILED callSid=%s", external_call_id)


def record_booking_outcome(external_call_id: str, *, outcome: str, booking_request_id=None) -> None:
    try:
        row = _get(external_call_id)
        if row is None:
            return
        row.booking_outcome = outcome[:40]
        if booking_request_id is not None:
            row.booking_request_id = booking_request_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_BOOKING_OUTCOME_FAILED callSid=%s", external_call_id)


def record_escalation(external_call_id: str) -> None:
    try:
        row = _get(external_call_id)
        if row is None:
            return
        row.escalated_to_human = True
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_ESCALATION_FAILED callSid=%s", external_call_id)


def record_usage(
    external_call_id: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    audio_input_seconds: int | None = None,
    audio_output_seconds: int | None = None,
) -> None:
    """Accumulates measured usage across a call's (possibly several)
    `response.done` events - OpenAI reports usage per response, not once
    for the whole call, so each event's figures are added to the running
    total rather than overwriting it."""
    try:
        row = _get(external_call_id)
        if row is None:
            return
        if input_tokens is not None:
            row.input_tokens = (row.input_tokens or 0) + input_tokens
        if output_tokens is not None:
            row.output_tokens = (row.output_tokens or 0) + output_tokens
        if cached_input_tokens is not None:
            row.cached_input_tokens = (row.cached_input_tokens or 0) + cached_input_tokens
        if audio_input_seconds is not None:
            row.audio_input_seconds = (row.audio_input_seconds or 0) + audio_input_seconds
        if audio_output_seconds is not None:
            row.audio_output_seconds = (row.audio_output_seconds or 0) + audio_output_seconds
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_USAGE_FAILED callSid=%s", external_call_id)


def finish_call(external_call_id: str, *, end_reason: str) -> None:
    """Closes out the call's measured duration and, from that plus the
    usage already accumulated by record_usage, calculates its estimated
    OpenAI and Twilio infrastructure cost - see app/ai_voice/pricing.py for
    what "estimated" means here and why neither figure is ever a
    provider-billed actual."""
    try:
        row = _get(external_call_id)
        if row is None:
            return
        row.ended_at = _now()
        row.duration_seconds = max(0, int((row.ended_at - row.started_at).total_seconds()))
        row.end_reason = end_reason

        model = current_app.config.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
        openai_amount, openai_version = pricing.calculate_openai_cost(
            model=model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_input_tokens=row.cached_input_tokens,
            duration_seconds=row.duration_seconds,
        )
        if openai_amount is not None:
            row.openai_cost_amount = openai_amount
            row.openai_cost_currency = "USD"
            row.openai_cost_is_estimated = True
            row.openai_pricing_version = openai_version

        twilio_amount, twilio_version = pricing.calculate_twilio_cost(
            duration_seconds=row.duration_seconds
        )
        if twilio_amount is not None:
            row.twilio_cost_amount = twilio_amount
            row.twilio_cost_currency = "USD"
            row.twilio_cost_is_estimated = True
            row.twilio_pricing_version = twilio_version

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_TELEMETRY_FINISH_FAILED callSid=%s", external_call_id)
