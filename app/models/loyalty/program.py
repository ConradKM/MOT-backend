"""A business's loyalty programme configuration.

One programme per garage (MVP) - a garage either has loyalty off, or has one
configured programme. The model still generalises beyond barbers: units are
"qualifying visits" for a VISIT programme today, but ``program_type`` leaves
room for a future SPEND (points-per-currency) programme without a migration
that reshapes the ledger.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage

# VISIT: 1 (or ``earn_per_visit``) unit per qualifying completed appointment.
# SPEND is reserved for a future points-per-currency programme; not built in
# this MVP so a half-finished points economy doesn't delay the visit demo.
PROGRAM_TYPE_VISIT = "VISIT"
PROGRAM_TYPES = (PROGRAM_TYPE_VISIT,)

# The only reward type built for this MVP - deterministic and safe to apply
# against a booking price without a pricing-combination engine. Percentage /
# free-service rewards are a natural follow-up (same reward_type slot).
REWARD_TYPE_FIXED_DISCOUNT = "FIXED_DISCOUNT"
REWARD_TYPES = (REWARD_TYPE_FIXED_DISCOUNT,)


class LoyaltyProgram(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "loyalty_programs"
    __table_args__ = (UniqueConstraint("garage_id", name="uq_loyalty_programs_garage_id"),)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Loyalty programme")
    # Customer-facing blurb only - never internal ledger/unit terminology.
    description: Mapped[str | None] = mapped_column(Text)

    program_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PROGRAM_TYPE_VISIT
    )
    # Units earned per qualifying completed visit. Kept as an int multiplier
    # rather than hardcoded 1 so "double stamp days" is a config change, not
    # a migration - not exposed in the MVP owner UI, but safe to leave as-is.
    earn_per_visit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Units required to unlock one reward cycle.
    threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    reward_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REWARD_TYPE_FIXED_DISCOUNT
    )
    reward_value_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GBP")

    # None/empty = every appointment type qualifies. Stored as a JSON array of
    # GarageAppointmentType id strings rather than an association table - this
    # is a small, rarely-changed allow-list, not a many-to-many relationship
    # anything else needs to query from the other side.
    qualifying_appointment_type_ids: Mapped[list[str] | None] = mapped_column(JSON)
    # Optional minimum appointment price (minor units) for a visit to qualify.
    min_spend_minor: Mapped[int | None] = mapped_column(Integer)

    garage: Mapped["Garage"] = relationship("Garage")

    def qualifying_type_ids(self) -> set[str] | None:
        if not self.qualifying_appointment_type_ids:
            return None
        return {str(v) for v in self.qualifying_appointment_type_ids}
