"""Stripe adapter - the only place in the codebase that imports the Stripe
SDK or touches ``stripe.*`` types/status strings directly. Domain code
(app/payments/service.py, routes, webhooks) depends only on
app/payments/providers/base.py's ``PaymentProvider`` interface and its
CoMaz-owned DTOs - every Stripe-native status string and event type is
mapped to CoMaz's own vocabulary right here, before it leaves this file.

Uses PaymentIntents + the Payment Element (see docs/PAYMENTS_SETUP.md) -
Stripe's standard, PCI-light integration: card details never touch our
backend, only a client_secret does, and only ever on its way *to* the
browser, never persisted (see BookingPayment's docstring).
"""

from flask import current_app

from .base import (
    CHECKOUT_MODE_EMBEDDED,
    WEBHOOK_ACCOUNT_UPDATED,
    WEBHOOK_PAYMENT_CANCELLED,
    WEBHOOK_PAYMENT_FAILED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    WEBHOOK_REFUND_UPDATED,
    WEBHOOK_UNHANDLED,
    Capabilities,
    PaymentProvider,
    PaymentProviderError,
    PaymentSessionResult,
    ProviderWebhookEvent,
    RefundResult,
    WebhookVerificationError,
)

# Stripe's own PaymentIntent.status vocabulary -> CoMaz's PAYMENT_STATUSES.
# Never imported or matched against anywhere outside this file.
_PAYMENT_STATUS_MAP = {
    "requires_payment_method": "REQUIRES_PAYMENT",
    "requires_confirmation": "REQUIRES_PAYMENT",
    "requires_action": "REQUIRES_PAYMENT",
    "processing": "PENDING",
    "requires_capture": "PENDING",
    "succeeded": "SUCCEEDED",
    "canceled": "CANCELLED",
}

# Stripe's own Refund.status vocabulary -> CoMaz's PAYMENT_STATUSES.
_REFUND_STATUS_MAP = {
    "pending": "REFUND_PENDING",
    "succeeded": "REFUNDED",
    "failed": "REFUND_FAILED",
    "canceled": "REFUND_FAILED",
}

# Stripe event type -> CoMaz's normalised webhook kind.
_EVENT_KIND_MAP = {
    "payment_intent.succeeded": WEBHOOK_PAYMENT_SUCCEEDED,
    "payment_intent.payment_failed": WEBHOOK_PAYMENT_FAILED,
    "payment_intent.canceled": WEBHOOK_PAYMENT_CANCELLED,
    "charge.refunded": WEBHOOK_REFUND_UPDATED,
    "refund.updated": WEBHOOK_REFUND_UPDATED,
    # Connect only - a connected account's own status changed, not a payment.
    # Arrives on the separate Connect webhook endpoint/secret - see
    # app/payments/webhooks.py::stripe_connect_webhook.
    "account.updated": WEBHOOK_ACCOUNT_UPDATED,
}


def _map_payment_status(stripe_status: str | None) -> str:
    return _PAYMENT_STATUS_MAP.get(stripe_status or "", "PENDING")


def _map_refund_status(stripe_status: str | None) -> str:
    return _REFUND_STATUS_MAP.get(stripe_status or "", "REFUND_PENDING")


def _client():
    import stripe

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


def _client_data(client_secret: str | None, connected_account_id: str | None = None) -> dict:
    """The only Stripe-specific fields ever safe to send a browser - the
    Payment Element needs both to initialise (see
    src/components/customer/payments/StripeCheckout.tsx). ``available_wallets``
    tells the frontend which wallet buttons the Payment Element may surface -
    it does not itself turn them on; Apple Pay additionally needs its domain
    verified in the Stripe Dashboard (see docs/PAYMENTS_PROVIDERS.md), which
    is an account-level, non-code step."""
    return {
        "client_secret": client_secret,
        "publishable_key": current_app.config.get("STRIPE_PUBLISHABLE_KEY") or None,
        # An account id is not a credential. Stripe.js needs it to operate in
        # the same connected-account context as the Direct Charge intent;
        # this is always server-derived, never accepted from the browser.
        "stripe_account_id": connected_account_id,
        "available_wallets": list(StripePaymentProvider.capabilities.supported_wallets),
    }


class StripePaymentProvider(PaymentProvider):
    name = "stripe"
    capabilities = Capabilities(
        supports_embedded_checkout=True,
        supports_refunds=True,
        supports_partial_refunds=True,
        supports_payment_cancellation=True,
        supports_webhooks=True,
        supports_idempotency=True,
        supports_saved_payment_methods=True,
        # Stripe's Payment Element auto-detects and offers these when the
        # browser/device supports them and (for Apple Pay) the deployment's
        # domain is verified with Stripe - see docs/PAYMENTS_PROVIDERS.md.
        supported_wallets=("apple_pay", "google_pay"),
    )

    def __init__(self, connected_account_id: str | None = None):
        # Set only for a Direct Charge against a business's own Stripe
        # Connect account (see app/payments/connect.py) - every SDK call
        # below passes it through as Stripe's ``stripe_account`` request
        # option, which is what actually makes the connected account (not
        # CoMaz's platform account) the merchant of record. ``None`` means
        # "the platform account itself" - only ever true for a garage with
        # no Connect account yet, which create_deposit_hold already refuses
        # to do (see app/payments/config.py::is_payments_configured).
        self._connected_account_id = connected_account_id

    def is_configured(self) -> bool:
        cfg = current_app.config
        return bool(cfg.get("STRIPE_SECRET_KEY")) and bool(cfg.get("STRIPE_WEBHOOK_SECRET"))

    def create_payment(self, *, amount_minor, currency, idempotency_key, metadata):
        stripe = _client()
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_minor,
                currency=currency.lower(),
                metadata=metadata,
                # Card only, via the Payment Element - no redirect-based
                # methods, which would need a return_url we don't have a
                # clean place to send the customer back to mid-wizard.
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                idempotency_key=idempotency_key,
                stripe_account=self._connected_account_id,
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return PaymentSessionResult(
            provider_payment_id=intent.id,
            status=_map_payment_status(intent.status),
            checkout_mode=CHECKOUT_MODE_EMBEDDED,
            provider_data=_client_data(intent.client_secret, self._connected_account_id),
            metadata=dict(intent.metadata or {}),
        )

    def get_payment_status(self, provider_payment_id):
        stripe = _client()
        try:
            intent = stripe.PaymentIntent.retrieve(
                provider_payment_id, stripe_account=self._connected_account_id
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return PaymentSessionResult(
            provider_payment_id=intent.id,
            status=_map_payment_status(intent.status),
            checkout_mode=CHECKOUT_MODE_EMBEDDED,
            provider_data=_client_data(intent.client_secret, self._connected_account_id),
            metadata=dict(intent.metadata or {}),
        )

    def cancel_payment(self, provider_payment_id):
        stripe = _client()
        try:
            intent = stripe.PaymentIntent.retrieve(
                provider_payment_id, stripe_account=self._connected_account_id
            )
            if intent.status in ("succeeded", "canceled"):
                return
            stripe.PaymentIntent.cancel(
                provider_payment_id, stripe_account=self._connected_account_id
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        stripe = _client()
        try:
            refund = stripe.Refund.create(
                payment_intent=provider_payment_id,
                amount=amount_minor,
                idempotency_key=idempotency_key,
                stripe_account=self._connected_account_id,
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return RefundResult(
            provider_refund_id=refund.id,
            status=_map_refund_status(refund.status),
            amount_minor=refund.amount,
        )

    def verify_webhook(self, payload, headers, *, webhook_secret=None):
        stripe = _client()
        signature = headers.get("Stripe-Signature")
        secret = webhook_secret or current_app.config["STRIPE_WEBHOOK_SECRET"]
        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise WebhookVerificationError(str(exc)) from exc

        data_object = event["data"]["object"]
        provider_payment_id = None
        provider_refund_id = None
        failure_message = None
        account = None
        provider_account_id = event.get("account")
        status = data_object.get("status")
        if event["type"] == "account.updated":
            # The Account object itself, not a payment - see
            # app/payments/connect.py::sync_account_from_webhook.
            account = {
                "account_id": data_object.get("id"),
                "charges_enabled": bool(data_object.get("charges_enabled")),
                "payouts_enabled": bool(data_object.get("payouts_enabled")),
                "details_submitted": bool(data_object.get("details_submitted")),
            }
            status = None
        elif event["type"].startswith("payment_intent."):
            provider_payment_id = data_object.get("id")
            status = _map_payment_status(status)
            if event["type"] == "payment_intent.payment_failed":
                failure_message = (data_object.get("last_payment_error") or {}).get("message")
        elif event["type"].startswith("charge.refund") or event["type"] == "refund.updated":
            provider_refund_id = data_object.get("id")
            provider_payment_id = data_object.get("payment_intent")
            status = _map_refund_status(status)

        return ProviderWebhookEvent(
            event_id=event["id"],
            kind=_EVENT_KIND_MAP.get(event["type"], WEBHOOK_UNHANDLED),
            provider_payment_id=provider_payment_id,
            provider_refund_id=provider_refund_id,
            status=status,
            failure_message=failure_message,
            raw=event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else dict(event),
            provider_account_id=provider_account_id,
            account=account,
        )
