import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.loyalty.discount_code import DiscountCode

# RESERVED: held by a booking that isn't confirmed yet (a deposit booking
# waiting on payment). CONSUMED: the booking is confirmed. RELEASED: the
# booking fell through (expired, rejected, cancelled, no-show) and the use is
# given back. RESERVED and CONSUMED both count against a code's limits.
REDEMPTION_RESERVED = "RESERVED"
REDEMPTION_CONSUMED = "CONSUMED"
REDEMPTION_RELEASED = "RELEASED"
REDEMPTION_STATUSES = (REDEMPTION_RESERVED, REDEMPTION_CONSUMED, REDEMPTION_RELEASED)
ACTIVE_REDEMPTION_STATUSES = (REDEMPTION_RESERVED, REDEMPTION_CONSUMED)


class DiscountRedemption(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One use of a discount code by one booking.

    Limits are enforced under a row lock on the code (see
    app/loyalty/service.py::redeem); for single-use codes the partial unique
    index below is the database's own backstop, so two simultaneous checkouts
    can never both hold the same loyalty code.
    """

    __tablename__ = "discount_redemptions"
    __table_args__ = (
        Index(
            "uq_discount_redemptions_single_use_active",
            "discount_code_id",
            unique=True,
            postgresql_where=text("single_use AND status IN ('RESERVED', 'CONSUMED')"),
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    discount_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("discount_codes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="SET NULL"), index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    # The penny discount actually given.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # The code's max_uses == 1 at redemption time - what the partial unique
    # index keys on.
    single_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    discount_code: Mapped["DiscountCode"] = relationship("DiscountCode")
