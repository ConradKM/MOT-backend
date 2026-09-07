"""A customer's request for a member of staff to ring them back.

Deliberately narrow - this is not a general task/CRM system, just "someone
asked to be called" plus enough context for whoever picks it up. See
app/conversation/actions.py::create_callback_request for how these get made
(from the conversation engine's CALLBACK_REQUEST intent) and
app/communications/routes.py for the staff-facing list/complete endpoints.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.conversation.conversation_session import ConversationSession
    from app.models.customer import Customer
    from app.models.garage import Garage

STATUS_PENDING = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"
STATUSES = (STATUS_PENDING, STATUS_COMPLETED, STATUS_CANCELLED)


class CallbackRequest(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "callback_requests"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL")
    )
    phone_number: Mapped[str] = mapped_column(String(40), nullable=False)
    # Free text on purpose - "preferred time" from a natural conversation
    # ("this afternoon", "after 5") is not a reliable datetime.
    reason: Mapped[str | None] = mapped_column(Text)
    preferred_time: Mapped[str | None] = mapped_column(String(100))

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_PENDING, server_default=STATUS_PENDING
    )
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversation_sessions.id", ondelete="SET NULL")
    )

    garage: Mapped["Garage"] = relationship("Garage")
    customer: Mapped["Customer | None"] = relationship("Customer")
    source_session: Mapped["ConversationSession | None"] = relationship("ConversationSession")
