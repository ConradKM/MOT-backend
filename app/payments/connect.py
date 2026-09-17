"""Stripe Connect onboarding - one Express connected account per business.

Mirrors app/communications/provisioning/subaccounts.py's shape (create once,
idempotent on the stored id; a status refresh reconciles with what Stripe
actually reports), but Connect account ids are not secrets the way a Twilio
auth token is - they're safe to store in plain text on GaragePaymentSettings
and safe to return to the garage's own staff.

Every function here talks to CoMaz's single *platform* Stripe account (the
one that owns STRIPE_SECRET_KEY) - creating and managing connected accounts
is always done as the platform, even though the resulting account then
charges its own customers directly (see app/payments/providers/
stripe_provider.py's ``stripe_account`` threading for that part).
"""

from __future__ import annotations

from flask import current_app

from app.extensions import db
from app.models.garage import Garage
from app.models.payments.garage_payment_settings import GaragePaymentSettings


class ConnectError(RuntimeError):
    """A Stripe Connect API call failed, or was attempted before it could
    succeed (no platform key, no connected account yet). Routes.py turns
    this into a 503/409, never a 500."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _client():
    import stripe

    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise ConnectError("Stripe is not configured for this deployment.", code="not_configured")
    stripe.api_key = secret_key
    return stripe


def _get_or_create_settings(garage: Garage) -> GaragePaymentSettings:
    settings = garage.payment_settings
    if settings is None:
        settings = GaragePaymentSettings(garage_id=garage.id, provider="stripe")
        db.session.add(settings)
        db.session.flush()
    return settings


def create_connected_account(garage: Garage) -> str:
    """Create (or return the existing) Express connected account for
    ``garage``. Idempotent: a second call for a garage that already has one
    just returns its id - never creates a duplicate account."""
    settings = _get_or_create_settings(garage)
    if settings.stripe_account_id:
        return settings.stripe_account_id

    stripe = _client()
    try:
        account = stripe.Account.create(
            type="express",
            country="GB",
            email=garage.email,
            metadata={"garage_id": str(garage.id)},
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
    except stripe.error.StripeError as exc:  # pragma: no cover - real API only
        raise ConnectError(str(exc), code=getattr(exc, "code", None)) from exc

    settings.stripe_account_id = account.id
    db.session.commit()
    return str(account.id)


def create_account_link(garage: Garage, *, return_url: str, refresh_url: str) -> str:
    """A one-time Stripe-hosted onboarding URL for ``garage``'s connected
    account. Short-lived - generate a fresh one for every "Connect with
    Stripe" click rather than caching it."""
    settings = garage.payment_settings
    if settings is None or not settings.stripe_account_id:
        raise ConnectError(
            "No connected account for this business yet - call create_connected_account first.",
            code="no_account",
        )

    stripe = _client()
    try:
        link = stripe.AccountLink.create(
            account=settings.stripe_account_id,
            return_url=return_url,
            refresh_url=refresh_url,
            type="account_onboarding",
        )
    except stripe.error.StripeError as exc:  # pragma: no cover - real API only
        raise ConnectError(str(exc), code=getattr(exc, "code", None)) from exc
    return str(link.url)


def _apply_account_fields(settings: GaragePaymentSettings, account: dict) -> None:
    settings.stripe_charges_enabled = bool(account.get("charges_enabled", False))
    settings.stripe_payouts_enabled = bool(account.get("payouts_enabled", False))
    settings.stripe_details_submitted = bool(account.get("details_submitted", False))
    # Deliberately the same signal as details_submitted, not a separate
    # Stripe field - "onboarding complete" from CoMaz's point of view means
    # the business finished the Stripe-hosted form, independent of whether
    # every capability has since cleared review.
    settings.stripe_onboarding_complete = settings.stripe_details_submitted


def refresh_connect_status(garage: Garage) -> GaragePaymentSettings:
    """Fetch live status from Stripe and sync it onto ``garage``'s settings
    row - called right after the onboarding-return redirect (Stripe's
    return_url proves nothing on its own; this is the actual source of
    truth) and by the garage-facing status endpoint."""
    settings = garage.payment_settings
    if settings is None or not settings.stripe_account_id:
        raise ConnectError("No connected account for this business yet.", code="no_account")

    stripe = _client()
    try:
        account = stripe.Account.retrieve(settings.stripe_account_id)
    except stripe.error.StripeError as exc:  # pragma: no cover - real API only
        raise ConnectError(str(exc), code=getattr(exc, "code", None)) from exc

    _apply_account_fields(settings, account)
    db.session.commit()
    return settings


def sync_account_from_webhook(account: dict) -> None:
    """Apply a verified Stripe Connect ``account.updated`` event's
    normalised payload (see app/payments/providers/stripe_provider.py's
    ``account`` field on ProviderWebhookEvent) onto whichever garage owns
    that connected account.

    A silent no-op for an account id that matches no garage - either a
    delivery for an account CoMaz never created (shouldn't happen, but is
    not this platform's problem to raise an error over), or one that was
    disconnected since.
    """
    account_id = account.get("account_id")
    if not account_id:
        return
    settings = GaragePaymentSettings.query.filter_by(stripe_account_id=account_id).first()
    if settings is None:
        return
    _apply_account_fields(settings, account)
    db.session.commit()
