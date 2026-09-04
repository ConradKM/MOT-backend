import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class Vehicle(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint(
            "garage_id", "registration_number", name="uq_vehicle_garage_registration"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    registration_number: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )
    make: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    year: Mapped[int | None] = mapped_column()
    current_mileage: Mapped[int | None] = mapped_column()
    mot_expiry_date: Mapped[date | None] = mapped_column(Date)

    # Soft-delete: a vehicle with MOT history/appointments is archived rather
    # than hard-deleted (see app/vehicles/routes.py) so that history survives.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    garage = relationship("Garage", back_populates="vehicles")
    customer = relationship("Customer", back_populates="vehicles")
    mot_records = relationship(
        "MOTRecord",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="MOTRecord.mot_date.desc()",
    )
    appointments = relationship("Appointment", back_populates="vehicle")

    @validates("registration_number")
    def normalize_registration_number(self, key, value):
        if value is None:
            return value
        return value.strip().upper().replace(" ", "")
