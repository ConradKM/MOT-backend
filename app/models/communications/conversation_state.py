"""Per-conversation staff state for the WhatsApp inbox.

WhatsApp "conversations" are derived on the fly from ``CommunicationLog``
rows (see app/communications/queries.py::list_conversations) - there is no
conversation table. This is the small amount of *staff-managed* state that
has nowhere else to live: whether a thread has been archived out of the
inbox, or soft-deleted by an owner. One optional row per (garage, phone);
its absence means "a normal, active inbox thread".

Nothing here ever touches Twilio or the message history - archiving or
deleting a conversation in CoMaz OS only changes what staff see, never what
was sent.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class WhatsAppConversationState(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "whatsapp_conversation_states"
    __table_args__ = (
        UniqueConstraint(
            "garage_id", "phone_e164", name="uq_whatsapp_conversation_states_garage_phone"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Plain E.164 (no "whatsapp:" prefix) - the same form the API surfaces.
    phone_e164: Mapped[str] = mapped_column(String(40), nullable=False)

    # Set => hidden from the Inbox filter; the thread and its history are
    # untouched and it can be restored.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Owner-only soft delete: hidden from every filter. History rows stay.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage: Mapped["Garage"] = relationship("Garage")
