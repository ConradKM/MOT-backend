"""Deposit/payment orchestration - the one place that ties together the
provider abstraction (app/payments/providers), the BookingPayment/
BookingRequest models, and audit logging.

Booking-request status flow for a deposit-required type (see
app/models/booking_request.py for the full status docstring):

    AWAITING_PAYMENT --(webhook: payment_intent.succeeded)--> PENDING
    AWAITING_PAYMENT --(hold expires unpaid)-----------------> EXPIRED

A non-deposit booking never enters AWAITING_PAYMENT at all - it's created
PENDING immediately, exactly as before this module existed (see
app/public_booking/routes.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.communications.events import BOOKING_REQUEST_CREATED, emit_event
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.payment import (
    PAYMENT_STATUSES_CHARGED,
    PAYMENT_TYPE_DEPOSIT,
    BookingPayment,
)
from app.models.payments.webhook_event import PaymentWebhookEvent
from app.payments.audit import record_payment_event
from app.payments.config import is_payments_configured
from app.payments.money import calculate_deposit_minor, minor_to_decimal
from app.payments.providers import get_provider
from app.payments.providers.base import PaymentProviderError, ProviderWebhookEvent

# Provider-native intent/refund status strings -> our domain PAYMENT_STATUSES.
# Shared across providers - Stripe's PaymentIntent status vocabulary is what
# the fake provider deliberately mirrors (see providers/fake.py) so this one
# map covers both.
_PROVIDER_STATUS_MAP = {
    "requires_payment_method": "REQUIRES_PAYMENT",
    "requires_confirmation": "REQUIRES_PAYMENT",
    "requires_action": "REQUIRES_PAYMENT",
    "processing": "PENDING",
    "requires_capture": "PENDING",
    "succeeded": "SUCCEEDED",
    "canceled": "CANCELLED",
}


def map_provider_status(provider_status: str | None) -> str:
    return _PROVIDER_STATUS_MAP.get(provider_status or "", "PENDING")


class PaymentUnavailableError(RuntimeError):
    """Deposits are configured on the appointment type but no payment
    provider is actually available right now - routes.py turns this into a
    503, never a 500."""


def payment_hold_deadline(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    minutes = current_app.config.get("PAYMENT_HOLD_MINUTES", 15)
    return now + timedelta(minutes=minutes)


def create_deposit_hold(
    *,
    garage,
    appointment_type: GarageAppointmentType,
    booking_request: BookingRequest,
) -> tuple[BookingPayment, str | None]:
    """Create the BookingPayment row + provider payment intent for an
    already-built, not-yet-committed AWAITING_PAYMENT ``booking_request``.

    Returns ``(payment, client_secret)``. Raises ``PaymentUnavailableError``
    if no provider is configured, and re-raises ``PaymentProviderError`` (with
    the payment row marked FAILED first) if the provider call itself fails -
    callers must roll back the booking request in that case rather than leave
    an orphaned AWAITING_PAYMENT hold with no way to ever pay it.
    """
    if not is_payments_configured():
        raise PaymentUnavailableError("Payments are not configured for this deployment yet.")

    # Guaranteed by validate_deposit_config at configuration time (see
    # app/appointments/types/routes.py) - deposit_required=True never
    # persists without both set. Asserted, not re-validated, so a type
    # checker can see these are no longer Optional here.
    assert appointment_type.deposit_type is not None
    assert appointment_type.deposit_value is not None

    amount_minor = calculate_deposit_minor(
        deposit_type=appointment_type.deposit_type,
        deposit_value=appointment_type.deposit_value,
        base_price=appointment_type.base_price,
    )

    payment_id = uuid.uuid4()
    payment = BookingPayment(
        id=payment_id,
        garage_id=garage.id,
        booking_request_id=booking_request.id,
        provider=current_app.config.get("PAYMENTS_PROVIDER", "stripe"),
        payment_type=PAYMENT_TYPE_DEPOSIT,
        currency=appointment_type.deposit_currency or "GBP",
        amount_minor=amount_minor,
        status="REQUIRES_PAYMENT",
    )
    db.session.add(payment)
    db.session.flush()

    provider = get_provider()
    try:
        intent = provider.create_payment_intent(
            amount_minor=amount_minor,
            currency=payment.currency,
            # Stable per payment row - a network retry of this same call
            # (not a new customer attempt) returns the same intent instead of
            # creating a second one; see PaymentProvider.create_payment_intent.
            idempotency_key=f"deposit-{payment_id}",
            metadata={
                "booking_request_id": str(booking_request.id),
                "garage_id": str(garage.id),
                "appointment_type_id": str(appointment_type.id),
            },
        )
    except PaymentProviderError as exc:
        payment.status = "FAILED"
        payment.failure_reason = str(exc)
        payment.failed_at = datetime.now(UTC)
        raise

    payment.provider_payment_id = intent.provider_payment_id
    payment.status = map_provider_status(intent.status)

    record_payment_event(
        garage_id=garage.id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.deposit.created",
        summary=f"Deposit intent created ({payment.currency} {minor_to_decimal(amount_minor)}).",
        details={"provider_payment_id": intent.provider_payment_id, "amount_minor": amount_minor},
    )

    return payment, intent.client_secret


def expire_stale_payment_holds(garage_id=None, now: datetime | None = None) -> int:
    """Flip every AWAITING_PAYMENT request whose hold has timed out to
    EXPIRED, cancelling its in-flight provider intent so it can't be paid
    after the fact. Safe/cheap to call defensively (see
    app/public_booking/routes.py, app/booking_requests/routes.py), mirroring
    expire_stale_booking_requests's own calling convention."""
    now = now or datetime.now(UTC)

    query = BookingRequest.query.filter(BookingRequest.status == "AWAITING_PAYMENT")
    if garage_id is not None:
        query = query.filter(BookingRequest.garage_id == garage_id)

    stale = query.filter(BookingRequest.payment_hold_expires_at < now).all()
    if not stale:
        return 0

    provider = get_provider()
    for booking_request in stale:
        booking_request.status = "EXPIRED"
        booking_request.payment_hold_expires_at = None

        payment = booking_request.active_payment
        if payment is not None and payment.status in ("REQUIRES_PAYMENT", "PENDING"):
            if payment.provider_payment_id:
                try:
                    provider.cancel_payment(payment.provider_payment_id)
                except PaymentProviderError:
                    current_app.logger.warning(
                        "Failed to cancel expired payment intent %s",
                        payment.provider_payment_id,
                    )
            payment.status = "CANCELLED"
            payment.cancelled_at = now
            record_payment_event(
                garage_id=booking_request.garage_id,
                booking_request_id=booking_request.id,
                payment_id=payment.id,
                action="payment.deposit.hold_expired",
                summary="Payment hold expired before completion; slot released.",
            )

    db.session.commit()
    return len(stale)


def refund_deposit(
    booking_request: BookingRequest, *, reason: str, initiated_by: str = "staff"
) -> BookingPayment | None:
    """Issue a full refund for ``booking_request``'s successful deposit
    payment, if it has one. No-op (returns None) if there's nothing to
    refund - safe to call unconditionally from the reject route.

    Full refunds only (see the deposit spec's Part 11/23) - no partial-refund
    policy is implemented; PARTIALLY_REFUNDED exists in PAYMENT_STATUSES for a
    future manual/partial path but nothing sets it today.
    """
    payment = booking_request.active_payment
    if payment is None or payment.status not in PAYMENT_STATUSES_CHARGED:
        return None
    if payment.status != "SUCCEEDED":
        # Already refunded/refunding/failed-to-refund - refuse a second
        # attempt rather than double-refund.
        return payment

    provider = get_provider()
    payment.status = "REFUND_PENDING"
    payment.refund_requested_at = datetime.now(UTC)
    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.refund.initiated",
        summary=f"Refund initiated ({reason}).",
        details={"reason": reason, "initiated_by": initiated_by},
    )

    # A SUCCEEDED payment always has a provider_payment_id - it's set the
    # moment the intent is created, before any status can advance past
    # REQUIRES_PAYMENT.
    assert payment.provider_payment_id is not None

    try:
        refund = provider.refund_payment(
            payment.provider_payment_id,
            amount_minor=payment.amount_minor,
            idempotency_key=f"refund-{payment.id}",
        )
    except PaymentProviderError as exc:
        payment.status = "REFUND_FAILED"
        payment.refund_failed_at = datetime.now(UTC)
        payment.refund_failure_reason = str(exc)
        record_payment_event(
            garage_id=payment.garage_id,
            booking_request_id=booking_request.id,
            payment_id=payment.id,
            action="payment.refund.failed",
            summary=f"Refund failed: {exc}",
        )
        db.session.commit()
        return payment

    payment.provider_refund_id = refund.provider_refund_id
    # Some providers/methods settle a refund immediately (fake provider,
    # many card refunds); others report it async via webhook - only mark
    # REFUNDED here when the provider already says so, otherwise stay
    # REFUND_PENDING until charge.refunded/refund.updated arrives.
    if refund.status == "succeeded":
        payment.status = "REFUNDED"
        payment.refunded_amount_minor = refund.amount_minor
        payment.refunded_at = datetime.now(UTC)
        record_payment_event(
            garage_id=payment.garage_id,
            booking_request_id=booking_request.id,
            payment_id=payment.id,
            action="payment.refund.succeeded",
            summary="Refund succeeded.",
        )

    db.session.commit()
    return payment


# --- webhooks --------------------------------------------------------------


def process_webhook(raw_payload: bytes, headers: dict) -> None:
    """Verify + apply one provider webhook delivery. Idempotent: a
    redelivered event (same provider event id) is a no-op, enforced at the
    database level (PaymentWebhookEvent.id is the provider's own event id, a
    primary key) rather than a racy check-then-insert."""
    provider = get_provider()
    event = provider.verify_webhook(raw_payload, headers)

    record = PaymentWebhookEvent(
        id=event.event_id,
        provider=provider.name,
        event_type=event.event_type,
        received_at=datetime.now(UTC),
        payload=event.raw,
    )
    db.session.add(record)
    try:
        db.session.flush()
    except IntegrityError:
        # Already recorded - either fully processed already, or a concurrent
        # delivery is processing it right now. Either way, this delivery
        # does nothing further; the provider gets a 200 either way (see
        # webhooks.py) so it stops retrying.
        db.session.rollback()
        return

    _dispatch(event)
    record.processed_at = datetime.now(UTC)
    db.session.commit()


def _dispatch(event: ProviderWebhookEvent) -> None:
    if event.event_type == "payment_intent.succeeded":
        _handle_payment_succeeded(event)
    elif event.event_type == "payment_intent.payment_failed":
        _handle_payment_failed(event)
    elif event.event_type == "payment_intent.canceled":
        _handle_payment_cancelled(event)
    elif event.event_type in ("charge.refunded", "refund.updated"):
        _handle_refund_updated(event)
    # Anything else (e.g. payment_intent.created, payment_intent.processing)
    # is recorded in PaymentWebhookEvent for audit but needs no state change.


def _find_payment(provider_payment_id: str | None) -> BookingPayment | None:
    if not provider_payment_id:
        return None
    return BookingPayment.query.filter_by(  # type: ignore[no-any-return]
        provider_payment_id=provider_payment_id
    ).first()


def _handle_payment_succeeded(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id)
    if payment is None or payment.status == "SUCCEEDED":
        return  # unknown intent, or already handled (duplicate/out-of-order)

    payment.status = "SUCCEEDED"
    payment.paid_at = datetime.now(UTC)

    booking_request = payment.booking_request
    if booking_request.status == "AWAITING_PAYMENT":
        booking_request.status = "PENDING"
        booking_request.payment_hold_expires_at = None
        # The booking only becomes visible/actionable to staff now - this is
        # the deposit-flow's equivalent of the plain submit path's
        # BOOKING_REQUEST_CREATED emit (see app/public_booking/routes.py).
        emit_event(BOOKING_REQUEST_CREATED, garage=booking_request.garage, booking_request=booking_request)

    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.deposit.succeeded",
        summary="Deposit payment succeeded.",
    )


def _handle_payment_failed(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id)
    if payment is None or payment.status in ("SUCCEEDED", "CANCELLED"):
        return

    payment.status = "FAILED"
    payment.failed_at = datetime.now(UTC)
    payment.failure_reason = event.failure_message or "Payment failed."

    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=payment.booking_request_id,
        payment_id=payment.id,
        action="payment.deposit.failed",
        summary=f"Deposit payment failed: {payment.failure_reason}",
    )
    # booking_request stays AWAITING_PAYMENT (if the hold hasn't expired) so
    # the customer can retry with a new payment attempt - see routes.py.


def _handle_payment_cancelled(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id)
    if payment is None or payment.status in ("SUCCEEDED", "CANCELLED"):
        return

    payment.status = "CANCELLED"
    payment.cancelled_at = datetime.now(UTC)
    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=payment.booking_request_id,
        payment_id=payment.id,
        action="payment.deposit.cancelled",
        summary="Deposit payment cancelled.",
    )


def _handle_refund_updated(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id)
    if payment is None or payment.status == "REFUNDED":
        return

    if event.status in ("succeeded", "success"):
        payment.status = "REFUNDED"
        payment.refunded_amount_minor = payment.amount_minor
        payment.refunded_at = datetime.now(UTC)
        action, summary = "payment.refund.succeeded", "Refund succeeded."
    elif event.status == "failed":
        payment.status = "REFUND_FAILED"
        payment.refund_failed_at = datetime.now(UTC)
        action, summary = "payment.refund.failed", "Refund failed."
    else:
        payment.status = "REFUND_PENDING"
        action, summary = "payment.refund.pending", "Refund pending."

    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=payment.booking_request_id,
        payment_id=payment.id,
        action=action,
        summary=summary,
    )
