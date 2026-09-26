"""Append-only loyalty ledger - the single source of truth for a customer's
loyalty balance. Never mutate a row after insert; corrections are new
ADJUSTMENT/REVERSAL rows, same principle as the payments audit log.

Idempotency is enforced at the database layer, not just in application code:
``uq_loyalty_ledger_program_source`` makes a second EARN insert for the same
(program, source_type, source_id) a constraint violation. Postgres treats
NULLs as distinct for uniqueness, so MANUAL adjustments (which have no
natural source_id) are never blocked by each other.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.loyalty.program import LoyaltyProgram

ENTRY_TYPE_EARN = "EARN"
ENTRY_TYPE_REDEEM = "REDEEM"
ENTRY_TYPE_ADJUSTMENT = "ADJUSTMENT"
ENTRY_TYPE_REVERSAL = "REVERSAL"
ENTRY_TYPES = (ENTRY_TYPE_EARN, ENTRY_TYPE_REDEEM, ENTRY_TYPE_ADJUSTMENT, ENTRY_TYPE_REVERSAL)

# What triggered the entry. APPOINTMENT covers both booked appointments and
# walk-in/queue visits - queue completion promotes to a real Appointment and
# fires the same APPOINTMENT_COMPLETED event (see app/queueing/service.py::
# complete_service), so a single source_type covers both without the ledger
# needing to know queueing exists.
SOURCE_TYPE_APPOINTMENT = "APPOINTMENT"
SOURCE_TYPE_REWARD = "REWARD"
SOURCE_TYPE_MANUAL = "MANUAL"
SOURCE_TYPES = (SOURCE_TYPE_APPOINTMENT, SOURCE_TYPE_REWARD, SOURCE_TYPE_MANUAL)

# Entry types counted toward a customer's lifetime progress (see
# app/loyalty/service.py::lifetime_units). REDEEM is deliberately excluded -
# redeeming a reward consumes the reward, not the visits that earned it, so
# progress toward the *next* reward stays mathematically correct.
PROGRESS_ENTRY_TYPES = (ENTRY_TYPE_EARN, ENTRY_TYPE_ADJUSTMENT, ENTRY_TYPE_REVERSAL)


class LoyaltyLedgerEntry(db.Model, PrimaryKeyMixin):  # type: ignore[name-defined]
    __tablename__ = "loyalty_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "program_id",
            "source_type",
            "source_id",
            "entry_type",
            name="uq_loyalty_ledger_program_source",
        ),
        db.Index("ix_loyalty_ledger_garage_customer", "garage_id", "customer_id"),
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

    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Positive for EARN/upward ADJUSTMENT/REVERSAL, negative for downward
    # ADJUSTMENT. REDEEM rows carry the programme's threshold as a negative
    # number purely for audit readability - REDEEM is excluded from the
    # progress sum, so its sign never affects a customer's balance.
    units: Mapped[int] = mapped_column(Integer, nullable=False)

    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Free string, not a FK - the referent (Appointment or LoyaltyReward id)
    # varies by source_type. NULL for MANUAL adjustments.
    source_id: Mapped[str | None] = mapped_column(String(64))

    # Who caused this entry, for MANUAL/REDEEM entries. NULL for system-earned
    # (appointment completion) entries.
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    program: Mapped["LoyaltyProgram"] = relationship("LoyaltyProgram")
    customer: Mapped["Customer"] = relationship("Customer")
    actor: Mapped["Employee | None"] = relationship("Employee")
