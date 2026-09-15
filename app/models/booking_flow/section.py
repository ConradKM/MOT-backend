import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.booking_flow.field import BookingFlowField
    from app.models.garage import Garage


class BookingFlowSection(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One step of what a business asks a customer while booking.

    There is exactly one section the platform owns and this table never
    describes: "Your details" (name, email, mobile). It is built in and
    non-removable because the platform needs it to create the account, send
    the confirmation and issue a booking reference. *Everything* else the
    customer is asked - including anything about a vehicle, a pet, a room, a
    garment, whatever the business actually books in - is a row here.

    That is the whole point: the booking page was previously hard-coded for a
    single industry, which meant a business that doesn't book vehicles in
    could not use the product at all.

    Resolution is two-level. A section with ``appointment_type_id`` NULL is
    part of the business's default workflow and is asked for every service. A
    section naming a service belongs to that service's own override. When a
    service has *any* section of its own, its override replaces the default
    entirely rather than appending to it - a partial override would make it
    impossible to drop a default section for one service, and "replace" is
    the rule that can express both.
    """

    __tablename__ = "booking_flow_sections"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL = part of the business-wide default workflow. CASCADE rather than
    # SET NULL: a service's override is meaningless once the service is gone,
    # and silently promoting it to the business default would start asking
    # every customer a question meant for one service.
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("garage_appointment_types.id", ondelete="CASCADE"),
        index=True,
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # Lets a business take a section out of the flow without deleting it, and
    # without losing the answers already collected against its fields.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    garage: Mapped["Garage"] = relationship("Garage")
    appointment_type: Mapped["GarageAppointmentType | None"] = relationship("GarageAppointmentType")
    fields: Mapped[list["BookingFlowField"]] = relationship(
        "BookingFlowField",
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="BookingFlowField.order",
    )
