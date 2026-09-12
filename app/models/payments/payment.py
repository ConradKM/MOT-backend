import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.booking_request import BookingRequest
    from app.models.garage import Garage

# REQUIRES_PAYMENT: a payment intent/session has been created with the
# provider and is waiting for the customer to complete it. PENDING: the
# customer has submitted payment details and the provider is processing (e.g.
# a bank-redirect method, or a card requiring extra authentication) - not
# every provider/method passes through this state. SUCCEEDED: the provider
# webhook confirmed the charge - authoritative, never set from the browser's
# own "success" signal (see app/payments/service.py). FAILED: the provider
# reported the attempt failed (declined, expired, authentication failed).
# CANCELLED: the hold was abandoned and swept before payment completed (see
# app/payments/service.py::expire_stale_payment_holds), or the booking was
# withdrawn before paying. REFUND_PENDING/REFUNDED/REFUND_FAILED and
# PARTIALLY_REFUNDED describe a refund issued against a SUCCEEDED payment
# (business rejection, or a manual staff refund) - see refund_* columns below.
PAYMENT_STATUSES = (
    "REQUIRES_PAYMENT",
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "REFUND_PENDING",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
    "REFUND_FAILED",
)

#: A payment that has actually taken the customer's money - refunding it is
#: meaningful. Everything else either never charged, or already resolved.
PAYMENT_STATUSES_CHARGED = ("SUCCEEDED", "REFUND_PENDING", "PARTIALLY_REFUNDED", "REFUND_FAILED")

PAYMENT_TYPE_DEPOSIT = "DEPOSIT"
PAYMENT_TYPES = (PAYMENT_TYPE_DEPOSIT,)


class BookingPayment(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One payment attempt against a booking request - almost always a
    deposit collected before a booking request can be submitted for review.

    Provider-agnostic by design (see app/payments/providers): this table only
    ever stores a provider name + its own opaque reference ids, amounts in
    minor units, and safe metadata - never card/account details. A customer
    who retries after a failed attempt gets a *new* row (linked to the same
    booking_request_id) rather than this one being reused, so the full
    attempt history is preserved.

    ``amount_minor`` is calculated server-side (app/payments/money.py) from
    the appointment type's deposit configuration *at the moment this row is
    created* and never recalculated - exactly like
    Appointment.price_at_booking - so a later change to that configuration
    can't alter what a customer already paid or was asked to pay.
    """

    __tablename__ = "booking_payments"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Set once the booking request is approved and a real Appointment exists -
    # nullable because a deposit is always paid *before* approval.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL")
    )

    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    # The provider's own id for this payment (Stripe PaymentIntent id, etc) -
    # nullable only for the instant between row creation and the provider call
    # returning (see app/payments/service.py::create_deposit_hold).
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True)

    payment_type: Mapped[str] = mapped_column(String(20), nullable=False, default=PAYMENT_TYPE_DEPOSIT)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GBP")
    # Minor units (pence for GBP) - never a float. The persisted, calculated-
    # once amount; see class docstring.
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="REQUIRES_PAYMENT", index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(500))

    # Safe, non-sensitive provider context (e.g. the payment method type, the
    # Stripe client_secret is NOT stored here - it's returned to the frontend
    # once and never persisted). Never card/account numbers.
    payment_metadata: Mapped[dict | None] = mapped_column(JSON)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- refund tracking (business rejection, or a manual staff refund) ----
    refund_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_refund_id: Mapped[str | None] = mapped_column(String(255))
    refunded_amount_minor: Mapped[int | None] = mapped_column(Integer)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_failure_reason: Mapped[str | None] = mapped_column(String(500))

    garage: Mapped["Garage"] = relationship("Garage")
    booking_request: Mapped["BookingRequest"] = relationship(
        "BookingRequest", back_populates="payments"
    )
    appointment: Mapped["Appointment | None"] = relationship("Appointment")

    @property
    def deposit_amount(self) -> Decimal:
        """Major-unit Decimal for display (e.g. Decimal("25.00")) - computed
        from amount_minor, never stored as a second source of truth."""
        return (Decimal(self.amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
