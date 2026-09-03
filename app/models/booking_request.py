import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

# PENDING: awaiting staff review. APPROVED: staff accepted it and the linked
# customer/vehicle/appointment rows were created. REJECTED: staff declined.
BOOKING_REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED")


class BookingRequest(db.Model, PrimaryKeyMixin, TimestampMixin):
    """An unauthenticated public booking submission, held for staff review.

    Deliberately a flat snapshot of what the public form collected - it never
    writes into customers/vehicles/appointments directly. Approval (see
    app/booking_requests) is what turns it into real records.
    """

    __tablename__ = "booking_requests"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", index=True
    )

    # --- what the public form submitted -------------------------------------
    customer_first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    customer_phone: Mapped[str | None] = mapped_column(String(40))

    vehicle_registration: Mapped[str] = mapped_column(String(20), nullable=False)
    vehicle_make: Mapped[str | None] = mapped_column(String(100))
    vehicle_model: Mapped[str | None] = mapped_column(String(100))
    vehicle_year: Mapped[int | None] = mapped_column(Integer)
    vehicle_mileage: Mapped[int | None] = mapped_column(Integer)

    # Optional: the garage may not have configured appointment types, or the
    # type may be deleted later - keep the request either way.
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("garage_appointment_types.id", ondelete="SET NULL")
    )
    preferred_date: Mapped[date] = mapped_column(Date, nullable=False)
    preferred_time: Mapped[time | None] = mapped_column(Time)
    preferred_employee_note: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    # --- staff review outcome --------------------------------------------
    reviewed_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staff_notes: Mapped[str | None] = mapped_column(Text)

    # Records created when the request was approved (nullable, SET NULL so
    # deleting one of them doesn't delete the request's history).
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL")
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL")
    )

    garage = relationship("Garage")
    appointment_type = relationship("GarageAppointmentType")
    reviewed_by = relationship("Employee")
    customer = relationship("Customer")
    vehicle = relationship("Vehicle")
    appointment = relationship("Appointment")
