"""Usage, cost and quality telemetry for one AI-handled voice call.

Separate from ``CommunicationLog`` on purpose: that table is one row per
communication *attempt* across every channel, kept deliberately narrow.
Voice call telemetry is wide (token counts, per-tool outcomes, cost
breakdowns) and specific to the OpenAI Realtime SIP path, so it lives in its
own table joined by ``external_call_id`` (OpenAI's ``call_id``) rather than
bloating the shared log with columns every other channel leaves null.

No transcript or customer-message content is ever stored here - only
counts, outcomes and timings. What a caller said belongs in
``CommunicationLog``/the conversation engine's own turn logging, not in a
billing/analytics table.

Every cost figure is paired with an ``..._is_estimated`` flag: a provider
that returns billed usage directly (tokens, audio seconds) yields a
*measured* figure; anything CoMaz computes itself by multiplying a public
rate card is explicitly marked estimated, so a report can never present a
guess as a bill.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.booking_request import BookingRequest
    from app.models.garage import Garage

PROVIDER_OPENAI_REALTIME_SIP = "openai_realtime_sip"

END_REASON_HANDOFF = "handoff_transfer"
END_REASON_HANGUP = "model_hangup"
END_REASON_CONNECTION_CLOSED = "connection_closed"
END_REASON_CRASH = "control_connection_crash"
END_REASONS = (
    END_REASON_HANDOFF,
    END_REASON_HANGUP,
    END_REASON_CONNECTION_CLOSED,
    END_REASON_CRASH,
)


class VoiceCallMetrics(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "voice_call_metrics"
    __table_args__ = (
        Index("ix_voice_call_metrics_garage_id_started_at", "garage_id", "started_at"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="SET NULL")
    )

    provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default=PROVIDER_OPENAI_REALTIME_SIP
    )
    # OpenAI's own call_id - the one identifier available at call-accept
    # time (see app/ai_voice/routes.py). No Twilio CallSid ever reaches this
    # backend for this path - the call's media never touches it.
    external_call_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Measured: wall-clock time this backend's own control connection was
    # open, computed from started_at/ended_at - never estimated.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    end_reason: Mapped[str | None] = mapped_column(String(40))

    # --- OpenAI usage - measured, straight from Realtime `response.usage` ---
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    audio_input_seconds: Mapped[int | None] = mapped_column(Integer)
    audio_output_seconds: Mapped[int | None] = mapped_column(Integer)

    # --- Cost - see module docstring on measured vs. estimated. Every
    # amount is paired with the exact rate-table version that produced it
    # (see app/ai_voice/pricing.py) - rates change over time, but a stored
    # amount never silently drifts to match a newer rate. Recalculating
    # under a new version is always possible from the raw usage above, and
    # always produces a new figure under a new version, never an overwrite
    # of the old one, unless a provider-reported figure is reconciled in
    # (see pricing.py's reconcile_* functions).
    # 6 decimal places, not 4: a single-digit-second call's Twilio share
    # (e.g. 3s at $0.0034/min = $0.00017) would round to noise at 4dp,
    # distorting an aggregate summed across many short calls. See
    # app/ai_voice/pricing.py's own rounding.
    openai_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    openai_cost_currency: Mapped[str | None] = mapped_column(String(3))
    openai_cost_is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    openai_pricing_version: Mapped[str | None] = mapped_column(String(60))
    twilio_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    twilio_cost_currency: Mapped[str | None] = mapped_column(String(3))
    twilio_cost_is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    twilio_pricing_version: Mapped[str | None] = mapped_column(String(60))

    # --- Tool calls / booking / escalation outcome - counts and short
    # labels only, never arguments or transcript content ---
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{"tool": "get_available_slots", "outcome": "succeeded", "latency_ms": 210}, ...]
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    booking_outcome: Mapped[str | None] = mapped_column(String(40))
    escalated_to_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    garage: Mapped["Garage"] = relationship("Garage")
    booking_request: Mapped["BookingRequest | None"] = relationship("BookingRequest")
