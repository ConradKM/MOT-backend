import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class AppointmentChecklistItem(db.Model, PrimaryKeyMixin, TimestampMixin):
    """One logged result against a snapshotted checklist step.

    label/is_compulsory/media_type/media_required_for_statuses are copied
    from the ChecklistTemplateItem at snapshot time (not live-linked), so
    later template edits never change an already-logged item.
    """

    __tablename__ = "appointment_checklist_items"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_checklist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("appointment_checklists.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Traceability only, see module docstring - the original template item
    # may since have been edited or deleted.
    checklist_template_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("checklist_template_items.id", ondelete="SET NULL"),
    )

    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    is_compulsory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    media_required_for_statuses: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, default=list
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NOT_CHECKED")
    notes: Mapped[str | None] = mapped_column(Text)
    completed_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("employees.id", ondelete="SET NULL"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage = relationship("Garage")
    appointment_checklist = relationship("AppointmentChecklist", back_populates="items")
    checklist_template_item = relationship("ChecklistTemplateItem")
    completed_by = relationship("Employee")
    media = relationship(
        "ChecklistItemMedia", back_populates="appointment_checklist_item",
        cascade="all, delete-orphan",
    )
