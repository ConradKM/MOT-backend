import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

# ACTIVE: offered normally. HIDDEN: temporarily not offered for new bookings
# (e.g. paused), but not otherwise final - the garage may re-enable it.
# DEPRECATED: retired for good. Both HIDDEN and DEPRECATED behave the same
# way today (excluded from new appointments unless explicitly requested via
# the status filter); the distinction is for the garage's own reference.
APPOINTMENT_TYPE_STATUSES = ("ACTIVE", "HIDDEN", "DEPRECATED")


class GarageAppointmentType(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A garage-defined kind of appointment (e.g. "MOT", "Full Service").

    Replaces the old fixed, global appointment_type enum - every garage
    now defines its own list. See migration 46c9ee69459d's successor for
    the temporary per-garage seed of the old enum values, tracked for
    removal in a follow-up issue once garages can build their own list
    from scratch.
    """

    __tablename__ = "garage_appointment_types"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")

    garage = relationship("Garage", back_populates="appointment_types")
    checklist_template = relationship(
        "ChecklistTemplate",
        back_populates="appointment_type",
        uselist=False,
        cascade="all, delete-orphan",
    )
    appointments = relationship("Appointment", back_populates="appointment_type")
