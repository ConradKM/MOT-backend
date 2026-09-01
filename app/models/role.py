import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

# "OWNER" is a reserved role name - every garage gets one at creation, it
# can't be renamed or deleted (see app/roles/routes.py), and having it
# assigned is what owner_required checks for. Every other role, including
# the "STAFF" one seeded alongside it, is a plain per-garage tag with no
# effect on permissions.
PROTECTED_ROLE_NAMES = ("OWNER",)

employee_roles = db.Table(
    "employee_roles",
    db.Column(
        "employee_id", Uuid, ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    ),
    db.Column("role_id", Uuid, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class Role(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("garage_id", "name", name="uq_roles_garage_id_name"),)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    garage = relationship("Garage", back_populates="roles")
    employees = relationship("Employee", secondary=employee_roles, back_populates="roles")

    @property
    def is_protected(self) -> bool:
        return self.name in PROTECTED_ROLE_NAMES
