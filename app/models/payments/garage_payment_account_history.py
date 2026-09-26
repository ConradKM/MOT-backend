"""Audit trail of a garage's *previous* Stripe Connect accounts.

Written only by :func:`app.payments.connect.reconnect_stripe_account` - the
Platform Admin recovery path for a connected account the current CoMaz Stripe
platform can no longer reach (see docs/STRIPE_CONNECT_SETUP.md and issue
#271). ``GaragePaymentSettings.stripe_account_id`` only ever holds the
*current* active account; this table is what lets support/audit answer "what
account did this business use before, and why did it change" without that
history being overwritten or lost. Existing ``BookingPayment`` rows already
carry their own ``provider_account_id`` snapshot, so payment history stays
correctly attributed to whichever account actually processed it regardless of
what this table or the garage's current settings say later.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage
    from app.models.platform.admin import PlatformAdmin


class GaragePaymentAccountHistory(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One row per Stripe Connect account a garage has been detached from."""

    __tablename__ = "garage_payment_account_history"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="stripe")
    # The account this garage used to be connected to - never overwritten,
    # never deleted, regardless of what happens to the garage's current
    # connected account afterwards.
    stripe_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Why it was detached (e.g. "Stripe account inaccessible to current
    # CoMaz platform - Platform Admin reconnect").
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    detached_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platform_admins.id", ondelete="SET NULL")
    )

    garage: Mapped["Garage"] = relationship("Garage")
    detached_by_admin: Mapped["PlatformAdmin | None"] = relationship("PlatformAdmin")
