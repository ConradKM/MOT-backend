import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.appointments.checklist_template_item import ChecklistTemplateItem
    from app.models.garage import Garage


class ChecklistTemplate(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
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

    garage: Mapped["Garage"] = relationship("Garage")
    appointment_type: Mapped["GarageAppointmentType"] = relationship(
        "GarageAppointmentType", back_populates="checklist_template"
    )
    items: Mapped[list["ChecklistTemplateItem"]] = relationship(
        "ChecklistTemplateItem",
        back_populates="checklist_template",
        cascade="all, delete-orphan",
        order_by="ChecklistTemplateItem.order",
    )
