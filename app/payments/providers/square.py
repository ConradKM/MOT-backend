"""Square adapter - the only place in the codebase that knows Square's payment
and refund status strings, event types, or webhook envelope. Domain code
(app/payments/service.py, routes, webhooks) sees only base.py's normalised
DTOs, exactly as for Stripe.

Square is a **two-phase** provider (``Capabilities.requires_client_token_
submission``): unlike a Stripe PaymentIntent there is no pre-created,
client-confirmed object. ``create_payment`` therefore charges nothing - it
returns the client-safe values the Web Payments SDK needs (application id,
location id, environment, amount) and no provider payment id. The browser
tokenises the card (running SCA inside tokenize()) and the backend charges that
single-use ``source_id`` with :meth:`charge_with_source` (Square
CreatePayment, ``autocomplete=true``). The authoritative outcome still comes
from the verified ``payment.updated`` webhook.

Constructed per merchant: ``merchant_id`` (snapshotted on the BookingPayment
row as ``provider_account_id``) selects whose token is used; tokens are
decrypted and lazily refreshed via app/payments/square_oauth.py at the moment
of each call, and a 401 triggers exactly one forced refresh and retry.

v1 is card payments only. Apple Pay / Google Pay through Square need their own
domain verification in the Square Developer Console - a follow-up, so
``supported_wallets`` is honestly empty.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from flask import current_app

from app.payments import square_api

from .base import (
    CHECKOUT_MODE_EMBEDDED,
    WEBHOOK_ACCOUNT_REVOKED,
    WEBHOOK_PAYMENT_CANCELLED,
    WEBHOOK_PAYMENT_FAILED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    WEBHOOK_REFUND_UPDATED,
    WEBHOOK_UNHANDLED,
    Capabilities,
    PaymentProvider,
    PaymentProviderError,
    PaymentSessionResult,
    ProviderNotConfigured,
    ProviderWebhookEvent,
    RefundResult,
    WebhookVerificationError,
)

#: Square limits ``idempotency_key`` to 45 characters.
IDEMPOTENCY_KEY_MAX = 45

SIGNATURE_HEADER = "x-square-hmacsha256-signature"

# Square Payment.status -> PAYMENT_STATUSES. APPROVED means authorised but
# not yet captured; with autocomplete=true it completes shortly after.
_PAYMENT_STATUS_MAP = {
    "APPROVED": "PENDING",
    "PENDING": "PENDING",
    "COMPLETED": "SUCCEEDED",
    "CANCELED": "CANCELLED",
    "FAILED": "FAILED",
}

# Square PaymentRefund.status -> PAYMENT_STATUSES.
_REFUND_STATUS_MAP = {
    "PENDING": "REFUND_PENDING",
    "COMPLETED": "REFUNDED",
    "REJECTED": "REFUND_FAILED",
    "FAILED": "REFUND_FAILED",
}

# A payment event's kind depends on the payment's own status, not only the
# event type: Square sends payment.created/payment.updated for every change.
_PAYMENT_EVENT_KIND_BY_STATUS = {
    "SUCCEEDED": WEBHOOK_PAYMENT_SUCCEEDED,
    "FAILED": WEBHOOK_PAYMENT_FAILED,
    "CANCELLED": WEBHOOK_PAYMENT_CANCELLED,
}
_PAYMENT_EVENT_TYPES = ("payment.created", "payment.updated")
_REFUND_EVENT_TYPES = ("refund.created", "refund.updated")
_REVOKED_EVENT_TYPE = "oauth.authorization.revoked"

# Square error codes worth a specific, customer-safe message. Anything else
# collapses to a generic one - Square's raw detail can be technical.
_CARD_ERROR_MESSAGES = {
    "CARD_DECLINED": "Your card was declined.",
    "GENERIC_DECLINE": "Your card was declined.",
    "INSUFFICIENT_FUNDS": "Your card was declined (insufficient funds).",
    "CVV_FAILURE": "The card's security code was incorrect.",
    "ADDRESS_VERIFICATION_FAILURE": "The card's postcode didn't match.",
    "INVALID_EXPIRATION": "The card's expiry date is invalid.",
    "EXPIRATION_FAILURE": "The card has expired.",
    "INVALID_CARD": "The card details are invalid.",
    "CARD_NOT_SUPPORTED": "This card isn't supported. Please try another card.",
    "TRANSACTION_LIMIT": "The card's transaction limit was reached.",
    "VOICE_FAILURE": "Your card was declined.",
    "CARD_DECLINED_VERIFICATION_REQUIRED": "Your bank needs to verify this payment. Please try again.",
    "AMOUNT_TOO_LOW": "This deposit is below the card network's minimum.",
}


def _map_payment_status(square_status: str | None) -> str:
    return _PAYMENT_STATUS_MAP.get(square_status or "", "PENDING")


def _map_refund_status(square_status: str | None) -> str:
    return _REFUND_STATUS_MAP.get(square_status or "", "REFUND_PENDING")


def square_idempotency_key(key: str) -> str:
    """``key`` itself when it fits Square's 45-character limit (the service's
    ``deposit-<uuid>`` = 44, ``refund-<uuid>`` = 43); otherwise a stable
    digest of it, so a retry with the same logical key always maps to the
    same Square key and can never double-charge."""
    if len(key) <= IDEMPOTENCY_KEY_MAX:
        return key
    return hashlib.sha256(key.encode()).hexdigest()[:IDEMPOTENCY_KEY_MAX]


def _error(exc: square_api.SquareApiError, fallback: str) -> PaymentProviderError:
    message = _CARD_ERROR_MESSAGES.get(exc.code or "", fallback)
    return PaymentProviderError(message, code=(exc.code or "square_error").lower())


class SquarePaymentProvider(PaymentProvider):
    name = "square"
    capabilities = Capabilities(
        supports_embedded_checkout=True,
        supports_refunds=True,
        supports_partial_refunds=True,
        # Nothing to cancel in the autocomplete flow: no payment exists until
        # the customer submits, and a submitted one completes immediately.
        supports_payment_cancellation=False,
        supports_webhooks=True,
        supports_idempotency=True,
        supports_saved_payment_methods=False,
        supported_wallets=(),
        requires_client_token_submission=True,
    )
    checkout_mode = CHECKOUT_MODE_EMBEDDED

    def __init__(self, merchant_id: str | None = None):
        self._merchant_id = merchant_id

    # --- configuration ------------------------------------------------

    def is_configured(self) -> bool:
        """Deployment-level readiness only (application credentials, webhook
        verification, token encryption). Whether one business's own Square
        connection is usable is app/payments/settings.py::provider_account_ready."""
        from app.payments.square_oauth import square_deployment_configured

        return square_deployment_configured()

    # --- authenticated calls -------------------------------------------

    def _call(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        from app.payments.square_oauth import SquareConnectError, access_token_for_merchant

        try:
            token, _settings = access_token_for_merchant(self._merchant_id)
            try:
                return square_api.request(method, path, bearer_token=token, json=json_body)
            except square_api.SquareApiError as exc:
                if exc.status != 401:
                    raise
                # One forced refresh + retry: the token may have been rotated
                # or expired early. A second 401 is a real auth failure.
                token, _settings = access_token_for_merchant(self._merchant_id, force_refresh=True)
                return square_api.request(method, path, bearer_token=token, json=json_body)
        except SquareConnectError as exc:
            raise ProviderNotConfigured(
                "Square isn't connected for this business right now.", code=exc.code
            ) from exc

    def _location_id(self) -> str:
        from app.payments.square_oauth import settings_for_merchant

        settings = settings_for_merchant(self._merchant_id)
        if settings is None or not settings.square_location_id:
            raise ProviderNotConfigured(
                "No Square location is selected for this business.", code="no_location"
            )
        return str(settings.square_location_id)

    # --- interface ------------------------------------------------------

    def create_payment(self, *, amount_minor, currency, idempotency_key, metadata):
        """Phase one: no Square call and no charge - just the client-safe
        values the Web Payments SDK needs to render the card form."""
        if not self._merchant_id:
            raise ProviderNotConfigured("Square isn't connected for this business.")
        return PaymentSessionResult(
            provider_payment_id=None,
            status="REQUIRES_PAYMENT",
            checkout_mode=CHECKOUT_MODE_EMBEDDED,
            provider_data={
                "application_id": current_app.config.get("SQUARE_APPLICATION_ID") or None,
                "location_id": self._location_id(),
                "environment": square_api.environment(),
                "amount_minor": amount_minor,
                # card.tokenize(verificationDetails) wants a major-unit string.
                "amount": f"{amount_minor / 100:.2f}",
                "currency": currency.upper(),
            },
            metadata=dict(metadata),
        )

    def charge_with_source(
        self,
        *,
        amount_minor,
        currency,
        source_id,
        verification_token,
        idempotency_key,
        reference=None,
    ):
        body: dict = {
            "source_id": source_id,
            "idempotency_key": square_idempotency_key(idempotency_key),
            "amount_money": {"amount": amount_minor, "currency": currency.upper()},
            "location_id": self._location_id(),
            "autocomplete": True,
        }
        if verification_token:
            body["verification_token"] = verification_token
        if reference:
            body["reference_id"] = str(reference)[:40]
        try:
            response = self._call("POST", "/v2/payments", json_body=body)
        except square_api.SquareApiError as exc:
            raise _error(
                exc, "The payment couldn't be completed. Please try another card."
            ) from exc
        return self._session(response.get("payment") or {})

    def get_payment_status(self, provider_payment_id):
        try:
            response = self._call("GET", f"/v2/payments/{provider_payment_id}")
        except square_api.SquareApiError as exc:
            raise _error(exc, "Could not fetch the Square payment.") from exc
        return self._session(response.get("payment") or {})

    def cancel_payment(self, provider_payment_id):
        """Only an APPROVED (authorised, not captured) payment can be
        cancelled in Square; with autocomplete there normally isn't one. A
        terminal payment is a safe no-op, as the interface requires."""
        if not provider_payment_id:
            return
        try:
            payment = self._call("GET", f"/v2/payments/{provider_payment_id}").get("payment") or {}
            if payment.get("status") != "APPROVED":
                return
            self._call("POST", f"/v2/payments/{provider_payment_id}/cancel", json_body={})
        except square_api.SquareApiError as exc:
            raise _error(exc, "Could not cancel the Square payment.") from exc

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        payment = None
        try:
            payment = self._call("GET", f"/v2/payments/{provider_payment_id}").get("payment") or {}
            currency = (payment.get("amount_money") or {}).get("currency") or "GBP"
            response = self._call(
                "POST",
                "/v2/refunds",
                json_body={
                    "idempotency_key": square_idempotency_key(idempotency_key),
                    "payment_id": provider_payment_id,
                    "amount_money": {"amount": amount_minor, "currency": currency},
                },
            )
        except square_api.SquareApiError as exc:
            raise _error(exc, "Square couldn't issue the refund.") from exc
        refund = response.get("refund") or {}
        return RefundResult(
            provider_refund_id=str(refund.get("id")),
            status=_map_refund_status(refund.get("status")),
            amount_minor=int((refund.get("amount_money") or {}).get("amount") or amount_minor),
        )

    def verify_webhook(self, payload, headers, *, webhook_secret=None):
        cfg = current_app.config
        key = webhook_secret or cfg.get("SQUARE_WEBHOOK_SIGNATURE_KEY")
        url = cfg.get("SQUARE_WEBHOOK_NOTIFICATION_URL")
        if not key or not url:
            raise WebhookVerificationError("Square webhook verification is not configured.")
        signature = _header(headers, SIGNATURE_HEADER)
        if not signature:
            raise WebhookVerificationError("Missing Square signature.")
        expected = base64.b64encode(
            hmac.new(key.encode(), url.encode() + payload, hashlib.sha256).digest()
        ).decode()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("Invalid Square signature.")

        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise WebhookVerificationError("Malformed Square webhook payload.") from exc
        if not isinstance(raw, dict) or not raw.get("event_id"):
            raise WebhookVerificationError("Malformed Square webhook payload.")

        return _normalise_event(raw)

    # --- helpers --------------------------------------------------------

    def _session(self, payment: dict) -> PaymentSessionResult:
        return PaymentSessionResult(
            provider_payment_id=payment.get("id"),
            status=_map_payment_status(payment.get("status")),
            checkout_mode=CHECKOUT_MODE_EMBEDDED,
            provider_data={},
            metadata={},
        )


def _header(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value or "")
    return ""


def _normalise_event(raw: dict) -> ProviderWebhookEvent:
    from app.payments.square_oauth import settings_for_merchant

    event_type = raw.get("type") or ""
    merchant_id = raw.get("merchant_id")
    data_object = (raw.get("data") or {}).get("object") or {}

    kind = WEBHOOK_UNHANDLED
    provider_payment_id = None
    provider_refund_id = None
    status = None
    failure_message = None

    # Tenant resolution by exact merchant id. An unknown merchant is recorded
    # (for audit/idempotency) as unhandled and acknowledged with 200 - a 5xx
    # would make Square retry it forever.
    known = settings_for_merchant(merchant_id) is not None

    if known and event_type in _PAYMENT_EVENT_TYPES:
        payment = data_object.get("payment") or {}
        provider_payment_id = payment.get("id")
        status = _map_payment_status(payment.get("status"))
        kind = _PAYMENT_EVENT_KIND_BY_STATUS.get(status, WEBHOOK_UNHANDLED)
        if status == "FAILED":
            failure_message = "Card payment failed."
    elif known and event_type in _REFUND_EVENT_TYPES:
        refund = data_object.get("refund") or {}
        provider_refund_id = refund.get("id")
        provider_payment_id = refund.get("payment_id")
        status = _map_refund_status(refund.get("status"))
        kind = WEBHOOK_REFUND_UPDATED
    elif known and event_type == _REVOKED_EVENT_TYPE:
        kind = WEBHOOK_ACCOUNT_REVOKED

    return ProviderWebhookEvent(
        event_id=str(raw["event_id"]),
        kind=kind,
        provider_payment_id=provider_payment_id,
        provider_refund_id=provider_refund_id,
        status=status,
        failure_message=failure_message,
        raw=raw,
        # Payment lookups filter on this (service.py::_find_payment), so a
        # Square payment event can only ever touch a payment that was charged
        # on this same merchant.
        provider_account_id=merchant_id,
    )
