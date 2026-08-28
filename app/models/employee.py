import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

ROLES = ("OWNER", "STAFF")


class Employee(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "employees"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(30),
        default="OWNER",
        nullable=False,
    )

    garage = relationship(
        "Garage",
        back_populates="employees",
    )
    appointments = relationship(
        "Appointment",
        back_populates="employee",
        cascade="all, delete-orphan",
    )

    def has_role(self, *role_names: str) -> bool:
        return self.role in role_names
