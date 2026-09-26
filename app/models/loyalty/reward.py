"""A single unlocked reward cycle for one customer on one programme.

One row per threshold reached (``cycle_number`` 1, 2, 3, ...) - not a mutable
"do they have a reward" flag - so redeeming reward #1 can never be confused
with unlocking reward #2, and reward value/currency are snapshotted at
creation so a later programme edit never rewrites history (see
app/loyalty/service.py for generation/redemption logic).

Status is intentionally just AVAILABLE -> REDEEMED (or -> CANCELLED) for this
MVP. A RESERVED state for in-progress booking checkout is a natural follow-up
once a reward needs to be held during a payment, but isn't wired yet - see
the module docstring in app/loyalty/service.py for the exact seam.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.loyalty.ledger import LoyaltyLedgerEntry
    from app.models.loyalty.program import LoyaltyProgram

REWARD_STATUS_AVAILABLE = "AVAILABLE"
REWARD_STATUS_REDEEMED = "REDEEMED"
REWARD_STATUS_CANCELLED = "CANCELLED"
REWARD_STATUSES = (REWARD_STATUS_AVAILABLE, REWARD_STATUS_REDEEMED, REWARD_STATUS_CANCELLED)


class LoyaltyReward(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "loyalty_rewards"
    __table_args__ = (
        UniqueConstraint(
            "program_id",
            "customer_id",
            "cycle_number",
            name="uq_loyalty_rewards_program_customer_cycle",
        ),
        db.Index("ix_loyalty_rewards_garage_customer_status", "garage_id", "customer_id", "status"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("loyalty_programs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=REWARD_STATUS_AVAILABLE)

    # Snapshot of LoyaltyProgram.threshold at the moment this cycle was
    # unlocked. Progress math (app/loyalty/service.py::get_customer_progress)
    # sums this column rather than multiplying reward count by the
    # *current* threshold, so editing "5 visits" to "10 visits" later can
    # never retroactively corrupt an already-consumed cycle's accounting.
    threshold_at_generation: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshotted from LoyaltyProgram at generation time.
    reward_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reward_value_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )
    redemption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("loyalty_ledger_entries.id", ondelete="SET NULL")
    )

    program: Mapped["LoyaltyProgram"] = relationship("LoyaltyProgram")
    customer: Mapped["Customer"] = relationship("Customer")
    redeemed_by: Mapped["Employee | None"] = relationship("Employee")
    redemption_ledger_entry: Mapped["LoyaltyLedgerEntry | None"] = relationship(
        "LoyaltyLedgerEntry"
    )
