import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

MEDIA_TYPES = ("PHOTO", "VIDEO")


class ChecklistItemMedia(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Attachment record for a checklist item.

    Schema only for now - no upload endpoint exists yet (tracked in the
    media-storage follow-up issue), so storage_key/uploaded_at stay NULL
    until that mechanism lands.
    """

    __tablename__ = "checklist_item_media"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_checklist_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("appointment_checklist_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_type: Mapped[str] = mapped_column(String(10), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage = relationship("Garage")
    appointment_checklist_item = relationship("AppointmentChecklistItem", back_populates="media")
