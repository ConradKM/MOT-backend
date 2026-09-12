"""Payments configuration gate - mirrors
app/communications/config.py::is_twilio_configured.

Deposits can be turned on per Appointment Type independently of whether a
payment provider is actually wired up: an unconfigured provider is a normal,
permanent, crash-free state (no merchant account yet), not an error. Every
call that would reach the provider checks this first and degrades to a
clear 503 rather than a 500 - see app/payments/routes.py.
"""

from flask import current_app


def is_payments_configured() -> bool:
    """Whether the configured provider has everything it needs to actually
    take a payment. The in-process fake provider (tests, and any deployment
    that hasn't set PAYMENTS_PROVIDER) is always "configured" - it has no
    external account to be missing."""
    cfg = current_app.config
    provider = cfg.get("PAYMENTS_PROVIDER", "stripe")

    if provider == "fake":
        return True

    if provider == "stripe":
        return bool(cfg.get("STRIPE_SECRET_KEY")) and bool(cfg.get("STRIPE_WEBHOOK_SECRET"))

    return False
