import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid
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

    # A deactivated account can't log in and existing tokens stop working
    # (see the JWT blocklist loader in app/__init__.py).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Access/refresh tokens issued before this instant are rejected - set on a
    # password reset so the reset also ends any live sessions.
    tokens_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
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
