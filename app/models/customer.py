import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.garage import Garage
    from app.models.vehicle import Vehicle


class Customer(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "customers"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))

    # Soft-delete: a customer with vehicles/appointments is archived rather
    # than hard-deleted (see app/customers/routes.py) so historical records
    # stay intact. Archived customers are hidden from the main list but stay
    # reachable by id (e.g. from an appointment's customer link).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Future SMS use (see app/models/reminder.py): True once a customer has
    # explicitly asked not to be texted. Not yet surfaced anywhere - no SMS is
    # sent today - but the flag exists so the Twilio phase has a place to
    # check consent from day one instead of retrofitting it.
    sms_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    garage: Mapped["Garage"] = relationship("Garage", back_populates="customers")
    vehicles: Mapped[list["Vehicle"]] = relationship(
        "Vehicle", back_populates="customer", cascade="all, delete-orphan"
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="customer", cascade="all, delete-orphan"
    )
