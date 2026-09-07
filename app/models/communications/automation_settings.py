"""Owner-editable communications *automation* toggles - deliberately a
separate table from ``GarageCommunicationSettings`` (Twilio resource
identifiers), which stays platform/CLI-only. Everything here is safe for a
garage OWNER to change: whether to send an automated message for a given
event, and whether the conversation engine is allowed to answer customers
automatically at all. None of it is a Twilio credential or resource id.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class GarageCommunicationAutomationSettings(
    db.Model,  # type: ignore[name-defined]
    PrimaryKeyMixin,
    TimestampMixin,
):
    __tablename__ = "garage_communication_automation_settings"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_garage_communication_automation_settings_garage_id"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    booking_ack_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    booking_confirmation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    reminder_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    reminder_hours_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default="24"
    )
    missed_call_ack_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The master switch for the conversational booking engine (WhatsApp inbound
    # -> app/conversation/engine.py). Off by default - a garage opts in,
    # rather than customers suddenly getting automated replies. Everything
    # else in this table only controls *outbound* transactional messages,
    # which is a smaller behaviour change than a bot answering customers.
    conversation_automation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    garage: Mapped["Garage"] = relationship("Garage")
