import uuid

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class ChecklistTemplate(db.Model, PrimaryKeyMixin, TimestampMixin):
    """One checklist template per appointment type.

    Snapshotted onto an AppointmentChecklist (and its items copied onto
    AppointmentChecklistItem rows) the first time a checklist is opened for
    an appointment of this type - later edits here never retroactively
    change an already-created instance.
    """

    __tablename__ = "checklist_templates"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garage_appointment_types.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    garage = relationship("Garage")
    appointment_type = relationship("GarageAppointmentType", back_populates="checklist_template")
    items = relationship(
        "ChecklistTemplateItem",
        back_populates="checklist_template",
        cascade="all, delete-orphan",
        order_by="ChecklistTemplateItem.order",
    )
