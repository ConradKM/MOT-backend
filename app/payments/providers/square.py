"""Square adapter skeleton - architecture only, no real integration yet.

Not connected to any Square account and never will fake one: every method
raises :class:`ProviderNotConfigured` (a subclass of ``PaymentProviderError``
- see base.py), and :meth:`is_configured` always returns ``False``, so
``is_payments_configured`` (app/payments/config.py) refuses to route any real
deposit to this adapter. See docs/PAYMENTS_PROVIDERS.md for what implementing
this for real involves.

Shape of a real implementation (for whoever builds it):

- ``create_payment`` -> Square's Payments API, most naturally paired with
  the Web Payments SDK on the frontend (an embedded card form, closer to
  Stripe's Payment Element than to PayPal's redirect) - checkout mode
  EMBEDDED.
- Idempotency: Square requires an ``idempotency_key`` on every
  payment-creation call natively - maps directly onto this interface's own
  parameter.
- Webhooks: Square signs webhook payloads with an HMAC-SHA256 scheme over
  the notification URL + body. Map Square's event ``type`` (e.g.
  ``payment.updated``, ``refund.updated``) to the ``WEBHOOK_*`` kinds in
  base.py - Square's payment/refund ``status`` fields (``COMPLETED``,
  ``FAILED``, ``CANCELED``, ``PENDING``) map fairly directly onto
  ``PAYMENT_STATUSES``.
- Refunds: Square's Refunds API supports partial refunds.
"""

from .base import (
    CHECKOUT_MODE_EMBEDDED,
    Capabilities,
    PaymentProvider,
    ProviderNotConfigured,
)

_NOT_IMPLEMENTED = (
    "Square is not yet implemented for CoMaz - this is an architecture "
    "placeholder only. See docs/PAYMENTS_PROVIDERS.md."
)


class SquarePaymentProvider(PaymentProvider):
    name = "square"
    capabilities = Capabilities(
        supports_embedded_checkout=True,
        supports_refunds=True,
        supports_partial_refunds=True,
        supports_payment_cancellation=True,
        supports_webhooks=True,
        supports_idempotency=True,
        supports_saved_payment_methods=True,
    )
    checkout_mode = CHECKOUT_MODE_EMBEDDED

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

    def verify_webhook(self, payload, headers):
        raise ProviderNotConfigured(_NOT_IMPLEMENTED)
