import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

MEDIA_TYPES = ("PHOTO", "VIDEO")


class ChecklistItemMedia(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A photo/video attached to a checklist item.

    The bytes live in object storage (see app/storage); this row holds only
    the reference. Lifecycle: created with `storage_key` set and
    `uploaded_at` NULL when an upload URL is issued, then `uploaded_at` /
    `size_bytes` are filled once the client confirms the upload landed.
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
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage = relationship("Garage")
    appointment_checklist_item = relationship(
        "AppointmentChecklistItem", back_populates="media"
    )

    @property
    def is_uploaded(self) -> bool:
        return self.uploaded_at is not None
