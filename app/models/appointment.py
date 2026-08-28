import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

APPOINTMENT_TYPES = ("MOT", "SERVICE", "MOT_AND_SERVICE", "REPAIR", "OTHER")
APPOINTMENT_STATUSES = ("BOOKED", "COMPLETED", "CANCELLED", "NO_SHOW")


class Appointment(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_garage_id_start_time", "garage_id", "start_time"),
    )

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

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    appointment_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="BOOKED", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    garage = relationship("Garage", back_populates="appointments")
    employee = relationship("Employee", back_populates="appointments")
    customer = relationship("Customer", back_populates="appointments")
    vehicle = relationship("Vehicle", back_populates="appointments")
