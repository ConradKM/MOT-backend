import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.customer import Customer

DISCOUNT_SOURCE_LOYALTY = "LOYALTY"
DISCOUNT_SOURCE_MANUAL = "MANUAL"
DISCOUNT_SOURCES = (DISCOUNT_SOURCE_LOYALTY, DISCOUNT_SOURCE_MANUAL)

# Only these two are stored. "Used" and "Expired" are derived from the
# redemptions and the dates (see app/loyalty/service.py::derived_status), so
# they can never disagree with the facts they summarise.
DISCOUNT_STATUS_ACTIVE = "ACTIVE"
DISCOUNT_STATUS_DISABLED = "DISABLED"
DISCOUNT_CODE_STATUSES = (DISCOUNT_STATUS_ACTIVE, DISCOUNT_STATUS_DISABLED)

# What happened to a loyalty code's reward email - shown in the Discounts
# list so staff know when to pass a code on by hand.
EMAIL_STATUS_SENT = "SENT"
EMAIL_STATUS_FAILED = "FAILED"
EMAIL_STATUS_NO_EMAIL = "NO_EMAIL"
EMAIL_STATUS_OPTED_OUT = "OPTED_OUT"


class DiscountCode(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """A code a customer can redeem at checkout or staff can apply on the
    appointment form - auto-issued by the loyalty rule (LOYALTY: personal,
    single-use) or created by hand as a promo (MANUAL: anyone, with optional
    date and usage limits). One model, one redemption path
    (app/loyalty/service.py::evaluate_code)."""

    __tablename__ = "discount_codes"
    __table_args__ = (
        # Case-, space- and dash-insensitive uniqueness within a business:
        # "summer-10", "SUMMER 10" and "SUMMER10" are one code.
        UniqueConstraint("garage_id", "code_normalized", name="uq_discount_codes_garage_code"),
        # Exactly-once loyalty issuing: a retried request, two racing
        # completions, or a completed -> reopened -> completed flap can
        # never issue the same milestone twice. NULLs are distinct, so
        # manual codes are unaffected.
        UniqueConstraint(
            "customer_id",
            "issued_for_visit_count",
            name="uq_discount_codes_customer_milestone",
        ),
        Index("ix_discount_codes_garage_id_created_at", "garage_id", "created_at"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # As shown and typed (upper case, may contain a dash, e.g. LOYAL-7KQ2M9XA).
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    # Upper case with spaces and dashes removed - what every lookup compares.
    code_normalized: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    discount_type: Mapped[str] = mapped_column(String(20), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DISCOUNT_STATUS_ACTIVE, server_default="ACTIVE"
    )

    # Set (and binding) for loyalty codes only: the one customer who may use it.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Null = unlimited. Loyalty codes are always 1.
    max_uses: Mapped[int | None] = mapped_column(Integer)
    max_uses_per_customer: Mapped[int | None] = mapped_column(Integer)

    # Loyalty only: the completed-visit count this code rewards, and the
    # completion that reached it. A plain id, not a foreign key: appointments
    # already reference discount_codes (the discount snapshot), and a cycle
    # would make the two tables impossible to create or drop in order.
    # Appointments are never hard-deleted (they cancel), so it stays valid.
    issued_for_visit_count: Mapped[int | None] = mapped_column(Integer)
    issued_for_appointment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )

    # Loyalty only: what happened to the reward email (EMAIL_STATUS_*), null
    # until an attempt was made.
    email_status: Mapped[str | None] = mapped_column(String(20))
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped["Customer | None"] = relationship("Customer")
