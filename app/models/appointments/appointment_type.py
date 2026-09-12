import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.checklist_template import ChecklistTemplate
    from app.models.garage import Garage

# ACTIVE: offered normally. HIDDEN: temporarily not offered for new bookings
# (e.g. paused), but not otherwise final - the garage may re-enable it.
# DEPRECATED: retired for good. Both HIDDEN and DEPRECATED behave the same
# way today (excluded from new appointments unless explicitly requested via
# the status filter); the distinction is for the garage's own reference.
APPOINTMENT_TYPE_STATUSES = ("ACTIVE", "HIDDEN", "DEPRECATED")

# FIXED: deposit_value is a GBP amount (e.g. 20.00 -> £20 deposit).
# PERCENTAGE: deposit_value is a percentage of base_price (e.g. 25 -> 25%).
# See app/payments/money.py::calculate_deposit for how these are turned into
# minor units at booking time.
DEPOSIT_TYPES = ("FIXED", "PERCENTAGE")


class GarageAppointmentType(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
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
    # How long this kind of appointment normally takes. Optional - when set,
    # creating an appointment of this type can omit end_time and have it
    # derived from start_time + this duration (see appointments/routes.py).
    default_duration_minutes: Mapped[int | None] = mapped_column(Integer)

    # --- deposit configuration (see app/payments) --------------------------
    # Off by default - existing and newly created appointment types never
    # require a deposit unless an owner explicitly turns it on, and turning
    # it on is itself harmless if no payment provider is configured yet (the
    # public booking flow reports 503 rather than 500 - see
    # app/payments/config.py::is_payments_configured).
    deposit_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deposit_type: Mapped[str | None] = mapped_column(String(20))
    # FIXED: a GBP amount. PERCENTAGE: a number 0-100 (not a 0-1 fraction) -
    # see AppointmentTypeSchema's validation.
    deposit_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # GBP only for now (see app/config.py::DEPOSIT_CURRENCY) - stored per-type
    # anyway so a future multi-currency garage doesn't need a schema change.
    deposit_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GBP")

    garage: Mapped["Garage"] = relationship("Garage", back_populates="appointment_types")
    checklist_template: Mapped["ChecklistTemplate | None"] = relationship(
        "ChecklistTemplate",
        back_populates="appointment_type",
        uselist=False,
        cascade="all, delete-orphan",
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="appointment_type"
    )
