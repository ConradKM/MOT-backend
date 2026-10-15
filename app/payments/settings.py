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


def resolve_provider_account_id(garage, provider_name: str) -> str | None:
    """The business's own account at ``provider_name`` that its deposits
    charge onto - a Stripe Connect account id or a Square merchant id - or
    ``None`` for providers with no per-business account (fake/PayPal) and for
    a business that hasn't connected one (see :func:`provider_account_ready`,
    which is what actually gates whether a deposit can be taken)."""
    settings = getattr(garage, "payment_settings", None)
    if settings is None:
        return None
    account_id = None
    if provider_name == "stripe":
        account_id = settings.stripe_account_id
    elif provider_name == "square":
        account_id = settings.square_merchant_id
    return str(account_id) if account_id else None


#: Backwards-compatible name from the Stripe-only era.
resolve_connected_account_id = resolve_provider_account_id


def stripe_connect_ready(garage) -> bool:
    """Whether this garage's own Stripe Connect account can actually accept
    a Direct Charge right now. CoMaz has no platform-account fallback for
    Stripe deposits - see the module docstring on Model A vs Model B in
    docs/PAYMENTS_PROVIDERS.md: Model B (per-business Connect accounts) is
    the only one this codebase charges through today."""
    settings = getattr(garage, "payment_settings", None)
    return bool(
        settings is not None and settings.stripe_account_id and settings.stripe_charges_enabled
    )


def square_connection_ready(garage) -> bool:
    """Whether this garage's Square merchant connection can take a deposit:
    connected, a location chosen, a usable/refreshable token - see
    app/payments/square_oauth.py::square_account_ready."""
    from app.payments.square_oauth import square_account_ready

    return square_account_ready(getattr(garage, "payment_settings", None))


# Per-business account readiness, by provider. A provider absent from this map
# has no per-business account (the fake test provider), so deployment-level
# readiness alone decides.
_ACCOUNT_READINESS = {
    "stripe": stripe_connect_ready,
    "square": square_connection_ready,
}


def provider_account_ready(garage, provider_name: str) -> bool:
    """Provider-neutral: can ``garage``'s own account at ``provider_name``
    take a deposit right now? Callers never branch on a provider name."""
    check = _ACCOUNT_READINESS.get(provider_name)
    return True if check is None else bool(check(garage))


def mark_provider_account_revoked(provider_name: str, account_id: str | None) -> bool:
    """A verified "access revoked" webhook for ``account_id`` - stop that
    business's deposits until it reconnects. Returns False when the account
    is unknown or the provider has no such concept."""
    if provider_name == "square":
        from app.payments.square_oauth import mark_revoked

        return mark_revoked(account_id)
    return False


#: Providers a business owner may pick in Settings -> Payments. Validated
#: against the registered adapters too, so a name here without an adapter is
#: never selectable. The fake (tests) and PayPal (skeleton) adapters are not
#: offered.
OWNER_SELECTABLE_PROVIDERS = ("stripe", "square")


class ProviderSelectionError(RuntimeError):
    """``reason``: ``unknown_provider`` (422) or ``payment_in_progress`` (409)."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def selectable_providers() -> tuple[str, ...]:
    from app.payments.providers import registered_provider_names

    registered = set(registered_provider_names())
    return tuple(name for name in OWNER_SELECTABLE_PROVIDERS if name in registered)


def payments_in_flight(garage_id, *, provider: str | None = None) -> bool:
    """Whether a deposit for this garage is mid-checkout right now (a payment
    REQUIRES_PAYMENT/PENDING, or a booking request AWAITING_PAYMENT) - while
    one is, switching or disconnecting the provider is refused. Expired holds
    are swept first so a stale hold can't block an owner forever."""
    from app.models.booking_request import BookingRequest
    from app.models.payments.payment import BookingPayment
    from app.payments.service import expire_stale_payment_holds

    expire_stale_payment_holds(garage_id=garage_id)
    payments = BookingPayment.query.filter(
        BookingPayment.garage_id == garage_id,
        BookingPayment.status.in_(("REQUIRES_PAYMENT", "PENDING")),
    )
    if provider is not None:
        payments = payments.filter(BookingPayment.provider == provider)
    if payments.first() is not None:
        return True
    if provider is not None:
        # A hold with no matching payment row belongs to some other provider.
        return False
    return (
        BookingRequest.query.filter_by(garage_id=garage_id, status="AWAITING_PAYMENT").first()
        is not None
    )


def set_garage_provider(garage, provider_name: str, *, employee=None):
    """Make ``provider_name`` this business's one active provider.

    Historical payments are unaffected: each BookingPayment keeps its own
    ``provider``/``provider_account_id`` and is refunded through those (see
    app/payments/service.py::refund_deposit), never the garage's current
    choice. Commits."""
    from app.extensions import db
    from app.models.payments.garage_payment_settings import GaragePaymentSettings
    from app.payments.audit import record_payment_event

    if provider_name not in selectable_providers():
        raise ProviderSelectionError(
            f"Unknown payment provider {provider_name!r}.", reason="unknown_provider"
        )
    previous = resolve_provider_name(garage)
    if previous == provider_name and garage.payment_settings is not None:
        return garage.payment_settings
    # Before taking the row lock: the hold sweep inside commits. Even if a new
    # checkout slipped in between, it is harmless - it keeps its own provider
    # snapshot for completion and refunds.
    if payments_in_flight(garage.id):
        raise ProviderSelectionError(
            "A customer is paying a deposit right now, so the payment provider can't be "
            "changed. Please try again in a few minutes.",
            reason="payment_in_progress",
        )
    settings = (
        db.session.query(GaragePaymentSettings)
        .filter_by(garage_id=garage.id)
        .with_for_update()
        .one_or_none()
    )
    if settings is None:
        settings = GaragePaymentSettings(garage_id=garage.id, provider=provider_name)
        db.session.add(settings)
    settings.provider = provider_name
    record_payment_event(
        garage_id=garage.id,
        action="payment.provider.changed",
        summary=f"Payment provider changed from {previous} to {provider_name}.",
        details={
            "from": previous,
            "to": provider_name,
            "employee_id": str(employee.id) if employee is not None else None,
        },
    )
    db.session.commit()
    return settings
