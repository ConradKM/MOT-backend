"""An OWNER's customised wording for one transactional message.

A garage with no row for a given ``key`` uses the built-in default text (see
app/conversation/templates.py::DEFAULT_TEMPLATES) - the same "optional row,
safe in-code fallback" pattern as MOTReminderSettings/GarageScheduleSettings.
Only ``{{variable}}`` substitution is supported (see templates.py) - there is
no code execution here, by design (Part 24/34 of the brief).
"""

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class GarageMessageTemplate(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "garage_message_templates"
    __table_args__ = (
        UniqueConstraint("garage_id", "key", name="uq_garage_message_templates_garage_key"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # One of app/conversation/templates.py::DEFAULT_TEMPLATES's keys, e.g.
    # "booking_acknowledgement". Not a DB enum (see Reminder/CommunicationLog
    # for the same "free text, extended in code" convention) - a new
    # template key never needs a migration, just a new default + UI entry.
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    garage = relationship("Garage")
