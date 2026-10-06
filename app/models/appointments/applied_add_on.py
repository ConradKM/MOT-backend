import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.add_on import AddOn
    from app.models.appointments.appointment import Appointment
    from app.models.booking_request import BookingRequest


class AppliedAddOnMixin:
    """An add-on as selected on one booking, snapshotted at selection time.

    Name, price and duration are copied off the AddOn the same way
    Appointment.price_at_booking snapshots the type's base_price: editing (or
    deleting) the catalogue add-on later must never rewrite what a past
    booking was charged. ``add_on_id`` is SET NULL for exactly that reason.
    """

    @declared_attr
    def garage_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
        )

    @declared_attr
    def add_on_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid, ForeignKey("add_ons.id", ondelete="SET NULL"), index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Per-unit deltas; the booking's total effect is quantity x each.
    price_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    duration_delta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    @declared_attr
    def add_on(cls) -> Mapped["AddOn | None"]:
        return relationship("AddOn")


class AppointmentAddOn(db.Model, PrimaryKeyMixin, TimestampMixin, AppliedAddOnMixin):  # type: ignore[name-defined]
    __tablename__ = "appointment_add_ons"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    appointment: Mapped["Appointment"] = relationship("Appointment", back_populates="add_ons")


class BookingRequestAddOn(db.Model, PrimaryKeyMixin, TimestampMixin, AppliedAddOnMixin):  # type: ignore[name-defined]
    """What a customer picked on the public booking page - carried onto the
    real appointment as AppointmentAddOn rows when the request is approved."""

    __tablename__ = "booking_request_add_ons"

    booking_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )

    booking_request: Mapped["BookingRequest"] = relationship(
        "BookingRequest", back_populates="add_ons"
    )
