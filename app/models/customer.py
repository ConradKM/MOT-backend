import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class Customer(db.Model, PrimaryKeyMixin, TimestampMixin):
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

    garage = relationship("Garage", back_populates="customers")
    vehicles = relationship(
        "Vehicle", back_populates="customer", cascade="all, delete-orphan"
    )
    appointments = relationship(
        "Appointment", back_populates="customer", cascade="all, delete-orphan"
    )
