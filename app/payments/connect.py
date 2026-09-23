"""Stripe Connect onboarding - one connected account per business.

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

from urllib.parse import urlparse

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


def _secret_key() -> str:
    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise ConnectError("Stripe is not configured for this deployment.", code="not_configured")
    return str(secret_key)


def _client():
    import stripe

    stripe.api_key = _secret_key()
    return stripe


def _v2_client():
    """A ``StripeClient`` instance for the v2 Core Accounts API - see
    create_connected_account. New-account creation and Account Link creation
    use this v2 client; status retrieval and established webhook/payment
    handling remain on the v1-compatible resource APIs."""
    import stripe

    return stripe.StripeClient(_secret_key())


def _get_or_create_settings(garage: Garage) -> GaragePaymentSettings:
    settings = garage.payment_settings
    if settings is None:
        settings = GaragePaymentSettings(garage_id=garage.id, provider="stripe")
        db.session.add(settings)
        db.session.flush()
    return settings


def create_connected_account(garage: Garage) -> str:
    """Create (or return the existing) connected account for ``garage``.
    Idempotent: a second call for a garage that already has one just
    returns its id - never creates a duplicate account.

    Creates new accounts through the Accounts v2 API (``/v2/core/accounts``)
    - Stripe's current guidance for all new Connect account creation; the v1
    Accounts API (``stripe.Account.create``) now warns that it's not
    recommended for new integrations. The account is configured to
    provide a Stripe-managed Direct Charges account (see
    docs/STRIPE_CONNECT_SETUP.md's "Accounts v2 migration" section for the
    full reasoning):

    - ``dashboard="full"`` - the connected business has its own Stripe
      Dashboard.  Accounts v2 requires this when Stripe collects fees and
      bears negative-balance losses; ``express`` instead requires CoMaz to
      accept both of those responsibilities.
    - ``defaults.responsibilities`` both ``"stripe"`` - Stripe collects its
      own processing fee directly from the connected account's charge and
      is liable for the account's negative balances (CoMaz requests no
      ``application_fee_amount`` and
      has never taken a platform cut - see app/payments/providers/
      stripe_provider.py).
    - ``configuration.merchant.capabilities.card_payments`` - the same
      capability a v1 Express account requested; nothing else CoMaz uses
      (payment method eligibility, e.g. wallets) depends on requesting a
      capability by name in v2.

    Existing connected accounts are never changed or replaced here; this
    only affects a garage connecting Stripe for the first time.
    """
    settings = _get_or_create_settings(garage)
    if settings.stripe_account_id:
        return settings.stripe_account_id

    import stripe

    client = _v2_client()
    try:
        account = client.v2.core.accounts.create(
            {
                "contact_email": garage.email,
                "display_name": garage.name,
                # Accounts v2 deliberately does not support an Express
                # dashboard with Stripe as both fee and loss collector.  That
                # was the source of production's
                # account_controller_unsupported_configuration rejection.
                # Full Dashboard access is the supported Stripe-managed
                # configuration for CoMaz's direct-charge merchant model.
                "dashboard": "full",
                "identity": {"country": "GB", "entity_type": "company"},
                "configuration": {
                    "merchant": {"capabilities": {"card_payments": {"requested": True}}}
                },
                "defaults": {
                    "currency": "gbp",
                    "locales": ["en-GB"],
                    "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
                },
                "metadata": {"garage_id": str(garage.id)},
            }
        )
    except stripe.StripeError as exc:  # pragma: no cover - real API only
        current_app.logger.warning(
            "STRIPE_CONNECT_ACCOUNT_CREATE_FAILED garage=%s code=%s request_id=%s",
            garage.id,
            getattr(exc, "code", None),
            getattr(exc, "request_id", None),
            exc_info=True,
        )
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

    client = _v2_client()
    try:
        # Accounts v2 must use Accounts v2 Account Links.  The previous
        # migration created a v2 account then called legacy
        # ``POST /v1/account_links``; Stripe rejects that mixed lifecycle,
        # which surfaced to owners only as the generic setup failure.
        link = client.v2.core.account_links.create(
            {
                "account": settings.stripe_account_id,
                "use_case": {
                    "type": "account_onboarding",
                    "account_onboarding": {
                        "configurations": ["merchant"],
                        "return_url": return_url,
                        "refresh_url": refresh_url,
                    },
                },
            }
        )
    except Exception as exc:  # pragma: no cover - real API / SDK only
        # V2 error classes have moved across the preview SDK releases in the
        # supported dependency range.  Preserve the safe route boundary for
        # every provider/SDK failure; the route logs the diagnostic detail.
        raise ConnectError(str(exc), code=getattr(exc, "code", None)) from exc
    return str(link.url)


def _apply_account_fields(settings: GaragePaymentSettings, account: dict) -> None:
    # stripe-python returns a StripeObject, not a dict.  It deliberately
    # rejects dict helpers such as ``.get()``; normalise it before reading
    # status fields so the post-onboarding status refresh cannot turn into a
    # 500 response.
    if hasattr(account, "to_dict"):
        account = account.to_dict()
    settings.stripe_charges_enabled = bool(account.get("charges_enabled", False))
    settings.stripe_payouts_enabled = bool(account.get("payouts_enabled", False))
    settings.stripe_details_submitted = bool(account.get("details_submitted", False))
    # Deliberately the same signal as details_submitted, not a separate
    # Stripe field - "onboarding complete" from CoMaz's point of view means
    # the business finished the Stripe-hosted form, independent of whether
    # every capability has since cleared review.
    settings.stripe_onboarding_complete = settings.stripe_details_submitted


def _booking_domain() -> str | None:
    hostname = urlparse(current_app.config.get("BOOKING_BASE_URL", "")).hostname
    return str(hostname) if hostname else None


def _register_payment_method_domain(stripe, settings: GaragePaymentSettings) -> None:
    """Apple Pay requires the public booking domain to be registered - per
    connected account, since Direct Charges make the connected account the
    merchant of record for Apple Pay's purposes, not just the platform
    account (see docs/STRIPE_CONNECT_SETUP.md's "Apple Pay, Google Pay, and
    Link" section, the manual step this used to require). Registering
    here - every time onboarding status is refreshed, once the account can
    actually take charges - means a garage never needs anyone to click
    through the Stripe Dashboard by hand for this.

    Deliberately never raises: a garage's Stripe status refresh (and the
    onboarding flow it's part of) must not fail because a wallet nicety
    couldn't be registered. Stripe's own API is idempotent for a domain
    that's already registered on this account, so this is safe to call on
    every refresh, not just the first one.
    """
    if not settings.stripe_charges_enabled:
        return
    domain = _booking_domain()
    if not domain:
        return
    try:
        stripe.PaymentMethodDomain.create(
            domain_name=domain, stripe_account=settings.stripe_account_id
        )
        current_app.logger.info(
            "PAYMENT_METHOD_DOMAIN_REGISTERED garage=%s account=%s domain=%s",
            settings.garage_id,
            settings.stripe_account_id,
            domain,
        )
    except Exception:  # see docstring: never block onboarding on this
        current_app.logger.warning(
            "PAYMENT_METHOD_DOMAIN_REGISTRATION_FAILED garage=%s account=%s domain=%s",
            settings.garage_id,
            settings.stripe_account_id,
            domain,
            exc_info=True,
        )


def get_wallet_domain_status(garage: Garage) -> dict | None:
    """Live Apple Pay/wallet readiness for ``garage``'s connected account -
    not persisted, always fetched fresh, since it's Stripe's own
    verification state (registering the domain does not mean Apple Pay is
    immediately usable - Stripe verifies domain ownership asynchronously).

    Returns ``None`` when there's nothing to check yet (no connected
    account, no charges capability, or the lookup itself failed - this is
    a wallet nicety, never allowed to break the caller). Otherwise:
    ``{"domain": ..., "enabled": ..., "apple_pay_status": ..., "apple_pay_status_details": ...}``
    """
    settings = garage.payment_settings
    if settings is None or not settings.stripe_account_id or not settings.stripe_charges_enabled:
        return None
    domain = _booking_domain()
    if not domain:
        return None

    try:
        stripe = _client()
        domains = stripe.PaymentMethodDomain.list(
            domain_name=domain, stripe_account=settings.stripe_account_id
        )
    except Exception:
        current_app.logger.warning(
            "PAYMENT_METHOD_DOMAIN_STATUS_LOOKUP_FAILED garage=%s account=%s domain=%s",
            settings.garage_id,
            settings.stripe_account_id,
            domain,
            exc_info=True,
        )
        return None

    data = domains.get("data") if hasattr(domains, "get") else domains["data"]
    if not data:
        return None
    record = data[0]
    if hasattr(record, "to_dict"):
        record = record.to_dict()
    apple_pay = record.get("apple_pay") or {}
    return {
        "domain": record.get("domain_name"),
        "enabled": record.get("enabled"),
        "apple_pay_status": apple_pay.get("status"),
        "apple_pay_status_details": (apple_pay.get("status_details") or {}).get("error_message"),
    }


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
    _register_payment_method_domain(stripe, settings)
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
    # Registers the Apple Pay domain the moment Stripe reports this account
    # can take charges, even if nobody ever opens Payments settings again -
    # see _register_payment_method_domain. Building the client can itself
    # fail (e.g. no Stripe key configured in this environment); that must
    # never break processing of a real, verified webhook event.
    try:
        _register_payment_method_domain(_client(), settings)
    except ConnectError:
        pass
    db.session.commit()
