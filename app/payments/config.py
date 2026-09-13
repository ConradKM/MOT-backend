"""Payments configuration gate - mirrors
app/communications/config.py::is_twilio_configured.

Deposits can be turned on per Appointment Type independently of whether a
payment provider is actually wired up: an unconfigured provider is a normal,
permanent, crash-free state (no merchant account yet), not an error. Every
call that would reach a provider checks this first and degrades to a clear
503 rather than a 500 - see app/public_booking/routes.py.
"""

from app.payments.providers import get_provider


def is_payments_configured(provider_name: str) -> bool:
    """Whether ``provider_name``'s adapter has everything it needs to
    actually take a payment right now.

    A single source of truth deliberately delegated to the adapter itself
    (:meth:`PaymentProvider.is_configured`) rather than duplicated here per
    provider - the in-process fake provider (tests) is always "configured";
    Stripe checks its own keys; the PayPal/Square skeletons always report
    "not configured" until someone actually implements them (see
    app/payments/providers/paypal.py, .../square.py)."""
    try:
        provider = get_provider(provider_name)
    except ValueError:
        return False
    return provider.is_configured()
