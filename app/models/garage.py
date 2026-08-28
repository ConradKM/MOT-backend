from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class Garage(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "garages"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(500))

    employees = relationship(
        "Employee", back_populates="garage", cascade="all, delete-orphan"
    )
    customers = relationship(
        "Customer", back_populates="garage", cascade="all, delete-orphan"
    )
    vehicles = relationship(
        "Vehicle", back_populates="garage", cascade="all, delete-orphan"
    )
    mot_records = relationship(
        "MOTRecord", back_populates="garage", cascade="all, delete-orphan"
    )
    appointments = relationship(
        "Appointment", back_populates="garage", cascade="all, delete-orphan"
    )
