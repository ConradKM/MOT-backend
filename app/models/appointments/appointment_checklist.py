import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem
    from app.models.appointments.checklist_template import ChecklistTemplate
    from app.models.garage import Garage


class AppointmentChecklist(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """The actual, logged checklist for one appointment.

    Created by snapshotting the appointment type's current ChecklistTemplate
    the first time this is opened - see AppointmentChecklistItem for how
    each step's data is copied rather than live-linked.
    """

    __tablename__ = "appointment_checklists"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Traceability only - which template this was snapshotted from. Not the
    # source of truth for the checklist's content once created (the items
    # below are).
    checklist_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("checklist_templates.id", ondelete="SET NULL"),
    )

    garage: Mapped["Garage"] = relationship("Garage")
    appointment: Mapped["Appointment"] = relationship("Appointment", back_populates="checklist")
    checklist_template: Mapped["ChecklistTemplate | None"] = relationship("ChecklistTemplate")
    items: Mapped[list["AppointmentChecklistItem"]] = relationship(
        "AppointmentChecklistItem",
        back_populates="appointment_checklist",
        cascade="all, delete-orphan",
        order_by="AppointmentChecklistItem.order",
    )
