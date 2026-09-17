from .base import PaymentProvider

_ADAPTERS = ("fake", "stripe", "paypal", "square")


def get_provider(name: str, *, connected_account_id: str | None = None) -> PaymentProvider:
    """The named payment provider adapter.

    Business/domain code (app/payments/service.py, routes, webhooks) only
    ever talks to the ``PaymentProvider`` interface returned here - never
    imports a provider SDK, or a provider name string, directly. Adding a
    new provider means adding one branch here and a new adapter module;
    nothing else in the codebase changes. See :func:`get_provider_for_garage`
    for the per-business-aware entry point most callers actually want.

    ``connected_account_id`` is Stripe-Connect-specific (a business's own
    connected account - see app/payments/connect.py) and ignored by every
    other adapter; passing it for a non-Stripe provider is simply a no-op,
    not an error, so callers that resolve it generically (e.g. from a
    payment row's own ``provider_account_id``) never need a provider-name
    branch of their own.
    """
    if name == "fake":
        from .fake import FakePaymentProvider

        return FakePaymentProvider()

    if name == "stripe":
        from .stripe_provider import StripePaymentProvider

        return StripePaymentProvider(connected_account_id=connected_account_id)

    if name == "paypal":
        from .paypal import PayPalPaymentProvider

        return PayPalPaymentProvider()

    if name == "square":
        from .square import SquarePaymentProvider

        return SquarePaymentProvider()

    raise ValueError(f"Unknown payment provider: {name!r} (expected one of {_ADAPTERS})")


def get_provider_for_garage(garage) -> PaymentProvider:
    """The adapter a specific garage's deposits should use - see
    app/payments/settings.py::resolve_provider_name for how that's decided.

    For Stripe, this also threads the garage's own Connect account id
    through so every call the returned adapter makes is a Direct Charge
    against that business's account, never CoMaz's platform account."""
    from app.payments.settings import resolve_connected_account_id, resolve_provider_name

    name = resolve_provider_name(garage)
    return get_provider(name, connected_account_id=resolve_connected_account_id(garage, name))


__all__ = ["PaymentProvider", "get_provider", "get_provider_for_garage"]
