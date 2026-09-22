"""Tenant-owned business knowledge that may be supplied to the voice agent."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class GarageVoiceFAQ(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """A business answer, deliberately separate from operational booking data.

    Rows are archived rather than deleted so an owner can safely remove an
    answer from the agent without losing the record of what was configured.
    """

    __tablename__ = "garage_voice_faqs"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(String(300), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    garage: Mapped["Garage"] = relationship("Garage", back_populates="voice_faqs")
