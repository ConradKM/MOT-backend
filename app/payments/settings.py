"""Per-business payment provider resolution.

The only thing that changes when a business is given (or moves off) its own
``GaragePaymentSettings`` row (app/models/payments/garage_payment_settings.py)
is which adapter :func:`app.payments.providers.get_provider` returns and
whether payments are allowed for that garage at all - every other piece of
domain logic (deposit calculation, capacity holds, refunds, booking
transitions) stays exactly the same regardless of provider.

A garage with no row behaves exactly as every garage did before this model
existed: the deployment-wide ``PAYMENTS_PROVIDER`` default, always enabled.
"""

from flask import current_app


def resolve_provider_name(garage) -> str:
    """Which adapter name this garage's deposits should use."""
    settings = getattr(garage, "payment_settings", None)
    if settings is not None:
        return str(settings.provider)
    return str(current_app.config.get("PAYMENTS_PROVIDER", "stripe"))


def payments_enabled_for_garage(garage) -> bool:
    """A garage-level kill switch, independent of any one appointment type's
    own ``deposit_required`` - see GaragePaymentSettings.enabled."""
    settings = getattr(garage, "payment_settings", None)
    if settings is not None:
        return bool(settings.enabled)
    return True
