from .base import PaymentProvider

_ADAPTERS = ("fake", "stripe", "paypal", "square")


def get_provider(name: str) -> PaymentProvider:
    """The named payment provider adapter.

    Business/domain code (app/payments/service.py, routes, webhooks) only
    ever talks to the ``PaymentProvider`` interface returned here - never
    imports a provider SDK, or a provider name string, directly. Adding a
    new provider means adding one branch here and a new adapter module;
    nothing else in the codebase changes. See :func:`get_provider_for_garage`
    for the per-business-aware entry point most callers actually want.
    """
    if name == "fake":
        from .fake import FakePaymentProvider

        return FakePaymentProvider()

    if name == "stripe":
        from .stripe_provider import StripePaymentProvider

        return StripePaymentProvider()

    if name == "paypal":
        from .paypal import PayPalPaymentProvider

        return PayPalPaymentProvider()

    if name == "square":
        from .square import SquarePaymentProvider

        return SquarePaymentProvider()

    raise ValueError(f"Unknown payment provider: {name!r} (expected one of {_ADAPTERS})")


def get_provider_for_garage(garage) -> PaymentProvider:
    """The adapter a specific garage's deposits should use - see
    app/payments/settings.py::resolve_provider_name for how that's decided."""
    from app.payments.settings import resolve_provider_name

    return get_provider(resolve_provider_name(garage))


__all__ = ["PaymentProvider", "get_provider", "get_provider_for_garage"]
