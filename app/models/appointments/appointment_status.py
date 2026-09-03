import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class GarageAppointmentStatus(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A garage-configurable label + colour for an appointment status.

    `Appointment.status` stays a plain string - this table just customises how
    each value is shown and lets a garage add its own. A garage with no rows
    here falls back to the built-in default set (see defaults.py), so the
    feature is opt-in and existing behaviour is unchanged.
    """

    __tablename__ = "garage_appointment_statuses"
    __table_args__ = (
        UniqueConstraint("garage_id", "key", name="uq_garage_appointment_statuses_garage_key"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The value stored in Appointment.status for this status.
    key: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    # A colour token the frontend maps to classes (e.g. "blue", "emerald").
    color: Mapped[str] = mapped_column(String(20), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Terminal states (COMPLETED / CANCELLED / NO_SHOW-like); informational.
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # One of the seven built-ins: its key can't change and it can't be deleted.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    garage = relationship("Garage")
