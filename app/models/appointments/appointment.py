import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_checklist import AppointmentChecklist
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.garage import Garage
    from app.models.vehicle import Vehicle

# REQUESTED/IN_PROGRESS/ACTION_NEEDED cover the appointment's day-to-day lifecycle in
# the staff app; CANCELLED/NO_SHOW are terminal states. Default stays BOOKED rather than
# REQUESTED - appointments are currently only ever created by staff (already confirmed),
# not submitted by customers - REQUESTED is for once public booking exists.
APPOINTMENT_STATUSES = (
    "REQUESTED",
    "BOOKED",
    "IN_PROGRESS",
    "COMPLETED",
    "ACTION_NEEDED",
    "CANCELLED",
    "NO_SHOW",
)


class Appointment(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "appointments"
    __table_args__ = (Index("ix_appointments_garage_id_start_time", "garage_id", "start_time"),)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional: an appointment doesn't have to be tied to a specific vehicle.
    # If the vehicle is later removed, keep the appointment and just drop the
    # reference rather than losing the booking.
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("vehicles.id", ondelete="SET NULL"),
        index=True,
    )
    # No ondelete clause (blocks deletion while in use, the default) - an
    # appointment type in use by real bookings shouldn't be deletable out
    # from under them; see appointment_types routes for the 409 this causes.
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garage_appointment_types.id"),
        nullable=False,
        index=True,
    )

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(30), default="BOOKED", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Snapshot of appointment_type.base_price at creation time - the type's
    # price may change later (see app/appointments/types), but a historical
    # appointment should keep showing what it actually cost. Duration doesn't
    # need an equivalent snapshot: start_time/end_time are already fixed at
    # creation and never move just because default_duration_minutes changes.
    price_at_booking: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    garage: Mapped["Garage"] = relationship("Garage", back_populates="appointments")
    employee: Mapped["Employee"] = relationship("Employee", back_populates="appointments")
    customer: Mapped["Customer"] = relationship("Customer", back_populates="appointments")
    vehicle: Mapped["Vehicle | None"] = relationship("Vehicle", back_populates="appointments")
    appointment_type: Mapped["GarageAppointmentType"] = relationship(
        "GarageAppointmentType", back_populates="appointments"
    )
    checklist: Mapped["AppointmentChecklist | None"] = relationship(
        "AppointmentChecklist",
        back_populates="appointment",
        uselist=False,
        cascade="all, delete-orphan",
    )
