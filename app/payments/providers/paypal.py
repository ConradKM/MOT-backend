"""PayPal adapter skeleton - architecture only, no real integration yet.

Not connected to any PayPal account and never will fake one: every method
raises :class:`ProviderNotConfigured` (a subclass of ``PaymentProviderError``
- see base.py), and :meth:`is_configured` always returns ``False``, so
``is_payments_configured`` (app/payments/config.py) refuses to route any real
deposit to this adapter. A business cannot be silently "using PayPal" today -
see docs/PAYMENTS_PROVIDERS.md for what implementing this for real involves.

Shape of a real implementation (for whoever builds it):

- ``create_payment`` -> PayPal Orders API v2, ``POST /v2/checkout/orders``
  with ``intent: CAPTURE``; the checkout mode is REDIRECT (the classic
  Smart Buttons flow) unless/until Advanced Card Processing (an embedded
  flow) is specifically wanted, which would need its own capability flag.
- Idempotency: PayPal's ``PayPal-Request-Id`` header.
- Webhooks: PayPal signs webhook payloads with a certificate-based scheme
  (``/v1/notifications/verify-webhook-signature``) - very different from
  Stripe's HMAC, but the same normalisation contract applies: map PayPal's
  event ``event_type`` (e.g. ``PAYMENT.CAPTURE.COMPLETED``,
  ``PAYMENT.CAPTURE.DENIED``, ``PAYMENT.CAPTURE.REFUNDED``) to the
  ``WEBHOOK_*`` kinds in base.py.
- Refunds: PayPal's Payments API refund endpoint, which does support partial
  refunds.
"""

from .base import (
    CHECKOUT_MODE_REDIRECT,
    Capabilities,
    PaymentProvider,
    ProviderNotConfigured,
)

_NOT_IMPLEMENTED = (
    "PayPal is not yet implemented for CoMaz - this is an architecture "
    "placeholder only. See docs/PAYMENTS_PROVIDERS.md."
)


class PayPalPaymentProvider(PaymentProvider):
    name = "paypal"
    # Conservative until a real implementation proves otherwise - PayPal's
    # Orders API does in fact support partial refunds and saved payment
    # methods (Vault), but nothing here has been built/tested against them.
    capabilities = Capabilities(
        supports_redirect_checkout=True,
        supports_refunds=True,
        supports_payment_cancellation=True,
        supports_webhooks=True,
        supports_idempotency=True,
    )
    checkout_mode = CHECKOUT_MODE_REDIRECT

    def is_configured(self) -> bool:
        return False

    def create_payment(self, *, amount_minor, currency, idempotency_key, metadata):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)

    def get_payment_status(self, provider_payment_id):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)

    def cancel_payment(self, provider_payment_id):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)

    def verify_webhook(self, payload, headers, *, webhook_secret=None):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)
