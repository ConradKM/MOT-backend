import uuid

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class AppointmentChecklist(db.Model, PrimaryKeyMixin, TimestampMixin):
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

    garage = relationship("Garage")
    appointment = relationship("Appointment", back_populates="checklist")
    checklist_template = relationship("ChecklistTemplate")
    items = relationship(
        "AppointmentChecklistItem",
        back_populates="appointment_checklist",
        cascade="all, delete-orphan",
        order_by="AppointmentChecklistItem.order",
    )
