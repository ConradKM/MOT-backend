"""A business's own incoming-call menu (IVR), played before any AI session.

Owner-editable (Settings > Phone menu), unlike ``GarageCommunicationSettings``
which is platform-controlled. No row, or ``enabled`` False, means the inbound
call path behaves exactly as it did before this table existed - see
app/communications/voice_webhooks.py::incoming_call.

``options`` is a JSON list rather than a child table: a menu is small (at most
one option per DTMF digit), always read and written whole, and its per-action
payload differs by action - see app/communications/ivr/actions.py for the
registry that validates every option shape. Adding an action type adds a
registry entry, never a column.
"""

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class GarageVoiceIvrSettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "garage_voice_ivr_settings"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Spoken before the options. NULL = "Thanks for calling <business name>."
    greeting: Mapped[str | None] = mapped_column(String(500))
    # [{"digit": "1", "label": "bookings", "prompt": null,
    #   "action": "AI_BOOKING", "target": null}, ...]
    options: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # What happens after max_attempts invalid/no-input tries, or when a
    # selected action is unavailable. Never REPEAT_MENU - it must terminate.
    fallback_action: Mapped[str] = mapped_column(
        String(30), nullable=False, default="HUMAN_TRANSFER", server_default="HUMAN_TRANSFER"
    )
    # UK E.164 destination for a fallback transfer (and for an AI that fails
    # or hands off). NULL = the platform-set voice_escalation_number.
    fallback_target: Mapped[str | None] = mapped_column(String(20))
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
