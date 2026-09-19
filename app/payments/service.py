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
from typing import cast

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
from app.payments.providers import get_provider, get_provider_for_garage
from app.payments.providers.base import (
    WEBHOOK_ACCOUNT_UPDATED,
    WEBHOOK_PAYMENT_CANCELLED,
    WEBHOOK_PAYMENT_FAILED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    WEBHOOK_REFUND_UPDATED,
    PaymentProviderError,
    PaymentSessionResult,
    ProviderWebhookEvent,
)
from app.payments.settings import (
    payments_enabled_for_garage,
    resolve_connected_account_id,
    resolve_provider_name,
)


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
) -> tuple[BookingPayment, PaymentSessionResult]:
    """Create the BookingPayment row + provider payment session for an
    already-built, not-yet-committed AWAITING_PAYMENT ``booking_request``.

    Which provider is used is entirely this garage's own choice (see
    app/payments/settings.py::resolve_provider_name) - nothing here assumes
    Stripe, or any other single provider.

    Returns ``(payment, session)``. Raises ``PaymentUnavailableError`` if
    payments are disabled for this garage or its provider isn't configured,
    and re-raises ``PaymentProviderError`` (with the payment row marked
    FAILED first) if the provider call itself fails - callers must roll back
    the booking request in that case rather than leave an orphaned
    AWAITING_PAYMENT hold with no way to ever pay it.
    """
    provider_name = resolve_provider_name(garage)
    if not payments_enabled_for_garage(garage) or not is_payments_configured(
        provider_name, garage=garage
    ):
        raise PaymentUnavailableError("Payments are not configured for this business yet.")

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
        provider=provider_name,
        # Snapshotted now, not re-resolved later - see BookingPayment.
        # provider_account_id's own docstring.
        provider_account_id=resolve_connected_account_id(garage, provider_name),
        payment_type=PAYMENT_TYPE_DEPOSIT,
        currency=appointment_type.deposit_currency or "GBP",
        amount_minor=amount_minor,
        status="REQUIRES_PAYMENT",
    )
    db.session.add(payment)
    db.session.flush()

    provider = get_provider_for_garage(garage)
    try:
        session = provider.create_payment(
            amount_minor=amount_minor,
            currency=payment.currency,
            # Stable per payment row - a network retry of this same call
            # (not a new customer attempt) returns the same session instead
            # of creating a second one; see PaymentProvider.create_payment.
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

    payment.provider_payment_id = session.provider_payment_id
    # Already normalised by the adapter - never a provider-native string.
    payment.status = session.status

    record_payment_event(
        garage_id=garage.id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.deposit.created",
        summary=f"Deposit session created via {provider_name} "
        f"({payment.currency} {minor_to_decimal(amount_minor)}).",
        details={"provider_payment_id": session.provider_payment_id, "amount_minor": amount_minor},
    )

    return payment, session


def expire_stale_payment_holds(garage_id=None, now: datetime | None = None) -> int:
    """Flip every AWAITING_PAYMENT request whose hold has timed out to
    EXPIRED, cancelling its in-flight provider intent so it can't be paid
    after the fact. Safe/cheap to call defensively (see
    app/public_booking/routes.py, app/booking_requests/routes.py), mirroring
    expire_stale_booking_requests's own calling convention.

    Before giving up on a hold, this re-checks the provider's own live status
    for its payment first (:func:`_reconcile_if_already_succeeded`) - a
    delayed or missed webhook must never leave a customer who genuinely paid
    with their booking marked EXPIRED and their payment marked CANCELLED
    while Stripe (or any other provider) shows the charge as SUCCEEDED. That
    split previously happened for real: a webhook delivery that crashed
    before recording success (see stripe_provider.py::verify_webhook's
    history) left the local payment row at REQUIRES_PAYMENT indefinitely,
    and this function would then cancel-and-expire a hold Stripe had already
    fulfilled, because ``cancel_payment`` no-ops on an already-succeeded
    intent (by design - it must never fight a real charge) but the caller
    never checked whether that no-op meant a real payment survived.
    """
    now = now or datetime.now(UTC)

    query = BookingRequest.query.filter(BookingRequest.status == "AWAITING_PAYMENT")
    if garage_id is not None:
        query = query.filter(BookingRequest.garage_id == garage_id)

    stale = query.filter(BookingRequest.payment_hold_expires_at < now).all()
    if not stale:
        return 0

    expired_count = 0
    for booking_request in stale:
        payment = booking_request.active_payment
        if payment is not None and _reconcile_if_already_succeeded(booking_request, payment, now):
            continue

        booking_request.status = "EXPIRED"
        booking_request.payment_hold_expires_at = None
        expired_count += 1

        if payment is not None and payment.status in ("REQUIRES_PAYMENT", "PENDING"):
            if payment.provider_payment_id:
                try:
                    # Whichever adapter (and, for Stripe, whichever connected
                    # account) actually created this session - a garage's
                    # provider/Connect account could in principle have
                    # changed since, so this must not re-resolve either
                    # fresh; both are snapshotted on the payment row itself.
                    get_provider(
                        payment.provider, connected_account_id=payment.provider_account_id
                    ).cancel_payment(payment.provider_payment_id)
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
    return expired_count


def _reconcile_if_already_succeeded(
    booking_request: BookingRequest, payment: BookingPayment, now: datetime
) -> bool:
    """``True`` (and the booking/payment already updated) if the provider's
    own live status for ``payment`` says it actually succeeded - the same
    outcome ``_handle_payment_succeeded`` would apply from a genuine webhook,
    applied here instead because that webhook was missed, delayed, or (as
    happened for real - see :func:`expire_stale_payment_holds`) crashed
    before it could record anything. Never raises - a provider error here
    just means "can't confirm either way", so the caller falls back to its
    normal expire-the-hold path unchanged."""
    if payment.status not in ("REQUIRES_PAYMENT", "PENDING") or not payment.provider_payment_id:
        return False
    try:
        live = get_provider(
            payment.provider, connected_account_id=payment.provider_account_id
        ).get_payment_status(payment.provider_payment_id)
    except PaymentProviderError:
        current_app.logger.warning(
            "Could not confirm live status of payment intent %s before expiring its hold",
            payment.provider_payment_id,
        )
        return False

    if live.status != "SUCCEEDED":
        return False

    payment.status = "SUCCEEDED"
    payment.paid_at = now
    booking_request.status = "PENDING"
    booking_request.payment_hold_expires_at = None
    emit_event(
        BOOKING_REQUEST_CREATED, garage=booking_request.garage, booking_request=booking_request
    )
    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.deposit.succeeded",
        summary="Deposit payment succeeded (reconciled while checking a hold's expiry - "
        "the confirming webhook was missed or delayed).",
    )
    return True


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

    # Whichever adapter (and connected account) actually created this
    # payment - see the same note in expire_stale_payment_holds above.
    provider = get_provider(payment.provider, connected_account_id=payment.provider_account_id)
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
    # Already normalised by the adapter. Some providers/methods settle a
    # refund immediately (fake provider, many card refunds); others report
    # it async via webhook - only mark REFUNDED here when the provider
    # already says so, otherwise stay REFUND_PENDING until a refund webhook
    # arrives (see _handle_refund_updated below).
    if refund.status == "REFUNDED":
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


def process_webhook(
    provider_name: str, raw_payload: bytes, headers: dict, *, webhook_secret: str | None = None
) -> None:
    """Verify + apply one provider webhook delivery. Idempotent: a
    redelivered event (same provider event id) is a no-op, enforced at the
    database level (PaymentWebhookEvent.id is the provider's own event id, a
    primary key) rather than a racy check-then-insert.

    ``provider_name`` comes straight from the webhook URL
    (``/api/webhooks/payments/<provider>`` - see app/payments/webhooks.py),
    since a webhook delivery carries no garage/business context until *after*
    its payload has been parsed - unlike every other call in this module,
    which resolves the provider from a specific garage or payment row.

    ``webhook_secret`` is passed through unchanged to the adapter - Stripe
    Connect events arrive on a separate endpoint/secret from ordinary
    platform events (see app/payments/webhooks.py::stripe_connect_webhook);
    every other adapter ignores it.
    """
    provider = get_provider(provider_name)
    event = provider.verify_webhook(raw_payload, headers, webhook_secret=webhook_secret)

    record = PaymentWebhookEvent(
        id=event.event_id,
        provider=provider.name,
        # The normalised kind, not the provider's raw event type/name - the
        # raw value is still fully recoverable from `payload` for audit.
        event_type=event.kind,
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
    """Dispatches purely on the adapter-normalised ``kind`` - never a
    provider-native event type/name. Every adapter maps its own vocabulary
    to these constants in its own ``verify_webhook`` (see
    app/payments/providers/base.py)."""
    if event.kind == WEBHOOK_PAYMENT_SUCCEEDED:
        _handle_payment_succeeded(event)
    elif event.kind == WEBHOOK_PAYMENT_FAILED:
        _handle_payment_failed(event)
    elif event.kind == WEBHOOK_PAYMENT_CANCELLED:
        _handle_payment_cancelled(event)
    elif event.kind == WEBHOOK_REFUND_UPDATED:
        _handle_refund_updated(event)
    elif event.kind == WEBHOOK_ACCOUNT_UPDATED:
        _handle_account_updated(event)
    # Anything else (WEBHOOK_UNHANDLED - e.g. Stripe's payment_intent.created/
    # processing) is recorded in PaymentWebhookEvent for audit but needs no
    # state change.


def _handle_account_updated(event: ProviderWebhookEvent) -> None:
    """A connected account's own status changed - not a payment. See
    app/payments/connect.py::sync_account_from_webhook, which owns the
    actual field-syncing logic (also called from the onboarding-return
    status refresh, so both paths share one implementation)."""
    if event.account is None:
        return
    from app.payments.connect import sync_account_from_webhook

    sync_account_from_webhook(event.account)


def _find_payment(
    provider_payment_id: str | None, provider_account_id: str | None = None
) -> BookingPayment | None:
    if not provider_payment_id:
        return None
    payment = cast(
        BookingPayment | None,
        BookingPayment.query.filter_by(provider_payment_id=provider_payment_id).first(),
    )
    # A verified Connect delivery can still only change a payment made on the
    # account named in that delivery.  This is deliberately not imposed on
    # fake/legacy platform events, which carry no account id.
    if provider_account_id is not None and (
        payment is None or payment.provider_account_id != provider_account_id
    ):
        return None
    return payment


def _handle_payment_succeeded(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id, event.provider_account_id)
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
        emit_event(
            BOOKING_REQUEST_CREATED, garage=booking_request.garage, booking_request=booking_request
        )

    record_payment_event(
        garage_id=payment.garage_id,
        booking_request_id=booking_request.id,
        payment_id=payment.id,
        action="payment.deposit.succeeded",
        summary="Deposit payment succeeded.",
    )


def _handle_payment_failed(event: ProviderWebhookEvent) -> None:
    payment = _find_payment(event.provider_payment_id, event.provider_account_id)
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
    payment = _find_payment(event.provider_payment_id, event.provider_account_id)
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
    payment = _find_payment(event.provider_payment_id, event.provider_account_id)
    if payment is None or payment.status == "REFUNDED":
        return

    # event.status is already normalised by the adapter (one of
    # PAYMENT_STATUSES) - never a provider-native refund status string.
    if event.status == "REFUNDED":
        payment.status = "REFUNDED"
        payment.refunded_amount_minor = payment.amount_minor
        payment.refunded_at = datetime.now(UTC)
        action, summary = "payment.refund.succeeded", "Refund succeeded."
    elif event.status == "REFUND_FAILED":
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
