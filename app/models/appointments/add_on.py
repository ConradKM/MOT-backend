import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType

# Same meanings as APPOINTMENT_TYPE_STATUSES: only ACTIVE add-ons can be
# newly selected, but a HIDDEN/DEPRECATED one already applied to an
# appointment stays on it (its price/duration are snapshotted there anyway -
# see AppliedAddOnMixin).
ADD_ON_STATUSES = ("ACTIVE", "HIDDEN", "DEPRECATED")

# Garage-configurable per add-on (1 = pick it at most once). The ceiling is a
# sanity bound on a price/duration multiplier, not a business rule.
ADD_ON_MAX_QUANTITY_LIMIT = 99


class AddOn(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """An optional extra on one appointment type (e.g. "Rush job"), adjusting
    the appointment's price and/or duration - either way, deltas are signed.

    Belongs to its appointment type exactly as ChecklistTemplate does, and
    goes with it on delete.
    """

    __tablename__ = "add_ons"
    __table_args__ = (
        CheckConstraint(
            f"max_quantity >= 1 AND max_quantity <= {ADD_ON_MAX_QUANTITY_LIMIT}",
            name="ck_add_ons_max_quantity_range",
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garage_appointment_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    price_delta: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    duration_delta_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Free-text label scoped to this add-on's appointment type: at most one
    # add-on per group may be selected on a single appointment. NULL never
    # conflicts with anything.
    exclusivity_group: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    appointment_type: Mapped["GarageAppointmentType"] = relationship(
        "GarageAppointmentType", back_populates="add_ons"
    )
