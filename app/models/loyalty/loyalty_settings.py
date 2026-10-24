import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

DISCOUNT_TYPE_PERCENTAGE = "PERCENTAGE"
DISCOUNT_TYPE_FIXED = "FIXED"
DISCOUNT_TYPES = (DISCOUNT_TYPE_PERCENTAGE, DISCOUNT_TYPE_FIXED)


class LoyaltySettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One business's loyalty rule: every ``visits_required``-th COMPLETED
    appointment issues the customer a personal, single-use discount code
    (see app/loyalty/issuing.py). A single repeating rule - no tiers.

    No row means "never configured", which behaves exactly like
    ``enabled=False``.
    """

    __tablename__ = "loyalty_settings"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    visits_required: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    discount_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DISCOUNT_TYPE_PERCENTAGE, server_default="PERCENTAGE"
    )
    discount_value: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("10.00"), server_default="10.00"
    )
    expiry_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    # When the programme was last switched on. The reconcile command only
    # catches up milestones reached by completions *after* this, so turning
    # the programme on can never mass-issue (or mass-email) codes for history.
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
