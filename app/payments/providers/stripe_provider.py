"""Stripe adapter - the only place in the codebase that imports the Stripe
SDK or touches ``stripe.*`` types directly. Domain code (app/payments/service.py,
routes, webhooks) depends only on app/payments/providers/base.py's
``PaymentProvider`` interface.

Uses PaymentIntents + the Payment Element (see docs/PAYMENTS_SETUP.md) -
Stripe's standard, PCI-light integration: card details never touch our
backend, only a client_secret does, and only ever on its way *to* the
browser, never persisted (see BookingPayment's docstring).
"""

from flask import current_app

from .base import (
    PaymentProvider,
    PaymentProviderError,
    ProviderPaymentIntent,
    ProviderRefund,
    ProviderWebhookEvent,
    WebhookVerificationError,
)


def _client():
    import stripe

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


class StripePaymentProvider(PaymentProvider):
    name = "stripe"

    def create_payment_intent(self, *, amount_minor, currency, idempotency_key, metadata):
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
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return ProviderPaymentIntent(
            provider_payment_id=intent.id,
            client_secret=intent.client_secret,
            status=intent.status,
            metadata=dict(intent.metadata or {}),
        )

    def retrieve_payment(self, provider_payment_id):
        stripe = _client()
        try:
            intent = stripe.PaymentIntent.retrieve(provider_payment_id)
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return ProviderPaymentIntent(
            provider_payment_id=intent.id,
            client_secret=intent.client_secret,
            status=intent.status,
            metadata=dict(intent.metadata or {}),
        )

    def cancel_payment(self, provider_payment_id):
        stripe = _client()
        try:
            intent = stripe.PaymentIntent.retrieve(provider_payment_id)
            if intent.status in ("succeeded", "canceled"):
                return
            stripe.PaymentIntent.cancel(provider_payment_id)
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        stripe = _client()
        try:
            refund = stripe.Refund.create(
                payment_intent=provider_payment_id,
                amount=amount_minor,
                idempotency_key=idempotency_key,
            )
        except stripe.error.StripeError as exc:  # pragma: no cover - real API only
            raise PaymentProviderError(str(exc), code=getattr(exc, "code", None)) from exc

        return ProviderRefund(
            provider_refund_id=refund.id,
            status=refund.status,
            amount_minor=refund.amount,
        )

    def verify_webhook(self, payload, headers):
        stripe = _client()
        signature = headers.get("Stripe-Signature")
        webhook_secret = current_app.config["STRIPE_WEBHOOK_SECRET"]
        try:
            event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise WebhookVerificationError(str(exc)) from exc

        data_object = event["data"]["object"]
        provider_payment_id = None
        provider_refund_id = None
        failure_message = None
        if event["type"].startswith("payment_intent."):
            provider_payment_id = data_object.get("id")
            if event["type"] == "payment_intent.payment_failed":
                failure_message = (data_object.get("last_payment_error") or {}).get("message")
        elif event["type"].startswith("charge.refund") or event["type"] == "refund.updated":
            provider_refund_id = data_object.get("id")
            provider_payment_id = data_object.get("payment_intent")

        return ProviderWebhookEvent(
            event_id=event["id"],
            event_type=event["type"],
            provider_payment_id=provider_payment_id,
            provider_refund_id=provider_refund_id,
            status=data_object.get("status"),
            failure_message=failure_message,
            raw=event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else dict(event),
        )
