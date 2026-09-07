"""Persistent state for one ongoing customer <-> CoMaz OS conversation.

One row per (garage, channel, customer phone number) *episode* - not one row
per message (individual turns are ``CommunicationLog`` rows, see
app/models/communications/communication_log.py). This is deliberately a
generic, explicit-fields-plus-JSON-context model rather than a bag of opaque
AI state: ``app/conversation/engine.py`` is the only thing that writes
``context``, and only with plain, inspectable values (ids, dates, strings) -
see that package's module docstring for the "never store hidden reasoning"
rule this is designed around.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.garage import Garage

CHANNEL_VOICE = "VOICE"
CHANNEL_WHATSAPP = "WHATSAPP"
CHANNELS = (CHANNEL_VOICE, CHANNEL_WHATSAPP)

STATUS_ACTIVE = "ACTIVE"
STATUS_EXPIRED = "EXPIRED"
STATUS_COMPLETED = "COMPLETED"
STATUS_HUMAN_HANDOFF = "HUMAN_HANDOFF"
STATUSES = (STATUS_ACTIVE, STATUS_EXPIRED, STATUS_COMPLETED, STATUS_HUMAN_HANDOFF)


class ConversationSession(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        # The engine's first job on any inbound message is "find my active
        # session for this garage+channel+number" - this is that lookup.
        Index(
            "ix_conversation_sessions_lookup",
            "garage_id",
            "channel",
            "customer_phone",
            "status",
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(40), nullable=False)
    # Resolved once the phone number is safely matched (see
    # app/conversation/actions.py::find_customer_by_phone) - null for an
    # unrecognised number, filled in the moment one is identified.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_ACTIVE, server_default=STATUS_ACTIVE
    )
    # The current top-level customer goal (a CREATE_BOOKING, CANCEL_APPOINTMENT,
    # ... constant from app/conversation/intents.py). Null before anything is
    # detected yet.
    intent: Mapped[str | None] = mapped_column(String(40))
    # Which question the engine is waiting on a reply to, e.g.
    # "AWAITING_DATE" / "AWAITING_VEHICLE" / "AWAITING_CONFIRMATION" - see
    # app/conversation/workflows.py. Null when nothing is pending.
    workflow_step: Mapped[str | None] = mapped_column(String(40))
    # Explicit, structured slot-filling state for the in-progress workflow -
    # appointment_type_id, preferred_date, preferred_time, vehicle_id,
    # offered_slots, booking_request_id, target_appointment_id, etc. Every key
    # is a plain JSON-safe value the workflow handlers themselves define and
    # read back - never free-form AI output.
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Set together whenever a human needs to pick this up - see
    # app/conversation/session_service.py::handoff_to_human. Cleared by
    # resume_automation().
    handoff_reason: Mapped[str | None] = mapped_column(String(200))

    # The most recent inbound provider message id this session has already
    # acted on - a duplicate webhook delivery of the same id is a no-op (see
    # app/conversation/engine.py's idempotency check) rather than a second
    # booking/reply.
    last_external_message_id: Mapped[str | None] = mapped_column(String(100))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    garage: Mapped["Garage"] = relationship("Garage")
    customer: Mapped["Customer | None"] = relationship("Customer")
