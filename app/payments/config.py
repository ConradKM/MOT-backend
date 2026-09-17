"""Payments configuration gate - mirrors
app/communications/config.py::is_twilio_configured.

Deposits can be turned on per Appointment Type independently of whether a
payment provider is actually wired up: an unconfigured provider is a normal,
permanent, crash-free state (no merchant account yet), not an error. Every
call that would reach a provider checks this first and degrades to a clear
503 rather than a 500 - see app/public_booking/routes.py.
"""

from app.payments.providers import get_provider


def is_payments_configured(provider_name: str, garage=None) -> bool:
    """Whether ``provider_name``'s adapter has everything it needs to
    actually take a payment right now.

    Deployment-level readiness (keys/webhook secret exist at all) is
    delegated to the adapter itself (:meth:`PaymentProvider.is_configured`)
    - the in-process fake provider (tests) is always "configured"; Stripe
    checks its own platform keys; the PayPal/Square skeletons always report
    "not configured" until someone actually implements them (see
    app/payments/providers/paypal.py, .../square.py).

    For Stripe specifically, that alone is not enough: CoMaz never charges a
    customer through its own platform account (see
    docs/PAYMENTS_PROVIDERS.md) - a ``garage`` must also have its own
    Connect account with charges actually enabled
    (app/payments/settings.py::stripe_connect_ready). Omitting ``garage``
    skips that check - used by call sites that aren't garage-specific (e.g.
    the webhook route, which must accept a delivery before it even knows
    which garage the payment belongs to)."""
    try:
        provider = get_provider(provider_name)
    except ValueError:
        return False
    if not provider.is_configured():
        return False
    if provider_name == "stripe" and garage is not None:
        from app.payments.settings import stripe_connect_ready

        return stripe_connect_ready(garage)
    return True
