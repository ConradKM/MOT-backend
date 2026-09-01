import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin
from .role import employee_roles


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

    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))

    password_hash: Mapped[str] = mapped_column(
        String(255),
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
    roles = relationship(
        "Role",
        secondary=employee_roles,
        back_populates="employees",
    )

    def has_role(self, *role_names: str) -> bool:
        names = {role.name for role in self.roles}
        return any(name in names for name in role_names)
