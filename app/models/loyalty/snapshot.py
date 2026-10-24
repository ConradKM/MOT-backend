"""The discount a booking was actually given, frozen onto the booking.

Shared by BookingRequest and Appointment - the same snapshotting philosophy
as ``price_at_booking``/``requested_price``: a later edit, disable or delete
of the code never changes what a historical booking says it cost.

On a discounted booking the row's own price column (``requested_price`` /
``price_at_booking``) is the *discounted* total - what the deposit is taken
from and what the customer owes - and ``price_before_discount`` is the
total before it. All six columns are null on a booking with no code.
"""

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


class DiscountSnapshotMixin:
    # SET NULL: deleting a code (only ever possible by deleting the garage or
    # customer it belongs to) must not take the booking's history with it -
    # the text/type/value/amount below still say what was applied.
    discount_code_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("discount_codes.id", ondelete="SET NULL")
    )
    discount_code: Mapped[str | None] = mapped_column(String(40))
    discount_type: Mapped[str | None] = mapped_column(String(20))
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_before_discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
