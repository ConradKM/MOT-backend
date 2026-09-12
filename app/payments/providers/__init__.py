from flask import current_app

from .base import PaymentProvider


def get_provider() -> PaymentProvider:
    """The configured payment provider adapter (PAYMENTS_PROVIDER).

    Business/domain code (app/payments/service.py, routes, webhooks) only
    ever talks to the ``PaymentProvider`` interface returned here - never
    imports the Stripe SDK, or a provider name string, directly. Adding a
    second provider means adding one branch here and a new adapter module;
    nothing else in the codebase changes.
    """
    name = current_app.config.get("PAYMENTS_PROVIDER", "stripe")

    if name == "fake":
        from .fake import FakePaymentProvider

        return FakePaymentProvider()

    if name == "stripe":
        from .stripe_provider import StripePaymentProvider

        return StripePaymentProvider()

    raise ValueError(f"Unknown PAYMENTS_PROVIDER: {name!r}")


__all__ = ["PaymentProvider", "get_provider"]
