import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

MEDIA_TYPES = ("NONE", "PHOTO", "VIDEO", "EITHER")

# Mirrors DVSA's MOT grading, extended for day-to-day service/repair work.
CHECKLIST_ITEM_STATUSES = (
    "PASS",
    "ADVISORY",
    "MINOR",
    "MAJOR",
    "DANGEROUS",
    "RECTIFIED",
    "RECOMMENDED",
    "CUSTOMER_DECLINED",
    "NOT_APPLICABLE",
    "NOT_CHECKED",
)


class ChecklistTemplateItem(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "checklist_template_items"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checklist_template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("checklist_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    is_compulsory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    # Which resulting statuses force media capture on this item. Empty means
    # media is always optional, regardless of the logged status.
    media_required_for_statuses: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, default=list
    )

    garage = relationship("Garage")
    checklist_template = relationship("ChecklistTemplate", back_populates="items")
