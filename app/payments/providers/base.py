"""The provider-agnostic payment interface.

Every payment provider adapter (stripe_provider.py, paypal.py, square.py,
fake.py, and any future one) implements this ABC. Domain code
(app/payments/service.py, routes, webhooks) depends only on the types in
this module - never on a provider SDK's own request/response shapes, and
never on a provider's own status/event vocabulary - so swapping or adding a
provider never touches booking/webhook/refund/capacity logic.

The normalisation boundary is the adapter itself: every method here returns
(or is given) CoMaz's own concepts - ``PAYMENT_STATUSES`` values, normalised
webhook ``kind``s - never a raw Stripe/PayPal/Square status string or event
type. A provider's own vocabulary must never leak past its own adapter file.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Re-exported here (not re-declared) so this module stays the single source
# of truth for "what is a valid payment status" without importing the model
# layer into the (lower-level) provider interface - see
# app/models/payments/payment.py, which imports nothing from here.
PAYMENT_STATUSES = (
    "REQUIRES_PAYMENT",
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "REFUND_PENDING",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
    "REFUND_FAILED",
)

# How the frontend should present the payment step for a given provider/
# session. EMBEDDED: a provider-hosted widget rendered inline (Stripe Payment
# Element, Square Web Payments SDK) - the browser never leaves the wizard.
# REDIRECT: the customer is sent to the provider's own hosted page and
# returns afterwards (classic PayPal Checkout). HOSTED is an alias kept
# distinct from REDIRECT for a provider that hosts the whole payment page
# without a full page navigation away from the app (e.g. an in-page modal
# iframe) - most providers fit EMBEDDED or REDIRECT; HOSTED exists so a
# future adapter isn't forced into either if it genuinely fits neither.
CHECKOUT_MODE_EMBEDDED = "EMBEDDED"
CHECKOUT_MODE_REDIRECT = "REDIRECT"
CHECKOUT_MODE_HOSTED = "HOSTED"
CHECKOUT_MODES = (CHECKOUT_MODE_EMBEDDED, CHECKOUT_MODE_REDIRECT, CHECKOUT_MODE_HOSTED)

# Normalised webhook event kinds - what app/payments/service.py dispatches
# on. Every adapter's verify_webhook() maps its own provider's event
# type/name to one of these; service.py never sees a raw Stripe event type
# string (or PayPal's/Square's own names) again.
WEBHOOK_PAYMENT_SUCCEEDED = "payment.succeeded"
WEBHOOK_PAYMENT_FAILED = "payment.failed"
WEBHOOK_PAYMENT_CANCELLED = "payment.cancelled"
WEBHOOK_REFUND_UPDATED = "refund.updated"
# A Connect connected account's status changed (onboarding progressed,
# charges_enabled/payouts_enabled flipped, a requirement was added) - not
# about any one payment. See app/payments/connect.py::sync_account_from_webhook.
WEBHOOK_ACCOUNT_UPDATED = "account.updated"
# Recorded (for audit/idempotency) but needs no state transition - e.g.
# Stripe's payment_intent.created/processing.
WEBHOOK_UNHANDLED = "unhandled"


@dataclass(frozen=True)
class Capabilities:
    """What a provider adapter can actually do. Purely descriptive metadata
    - nothing here gates behaviour on its own; a caller that needs a specific
    capability calls :meth:`require`, which raises a clear, typed error
    instead of the booking flow silently doing the wrong thing (e.g. trying
    to partially refund a provider that only supports full refunds)."""

    supports_embedded_checkout: bool = False
    supports_redirect_checkout: bool = False
    supports_refunds: bool = False
    supports_partial_refunds: bool = False
    supports_payment_cancellation: bool = False
    supports_webhooks: bool = False
    supports_idempotency: bool = False
    supports_saved_payment_methods: bool = False
    # Wallet payment methods (e.g. "apple_pay", "google_pay") this provider
    # can surface inside its own checkout - purely descriptive, like every
    # other field here: it does not itself enable anything. A provider that
    # is not configured (PayPal/Square skeletons) must always report an
    # empty tuple, never a wallet it cannot actually process.
    supported_wallets: tuple[str, ...] = ()

    def require(self, capability: str) -> None:
        if not getattr(self, capability, False):
            raise UnsupportedCapability(
                f"This payment provider does not support {capability.replace('_', ' ')}."
            )


@dataclass
class PaymentSessionResult:
    """What a provider hands back after creating (or re-fetching) a payment
    session - the CoMaz-owned shape every adapter normalises into.

    ``status`` is always one of ``PAYMENT_STATUSES`` (never a provider-native
    string - the adapter has already mapped it). ``provider_data`` is the
    *only* place any provider-specific, client-safe field lives (e.g.
    Stripe's ``client_secret``, a future redirect ``approval_url``) - it is
    handed to the frontend inside a generic envelope
    (``{provider, checkout_mode, payment_id, provider_data}``, see
    app/public_booking/schemas.py::DepositIntentCreatedSchema) and must never
    contain anything that isn't already safe to show a customer.
    """

    provider_payment_id: str
    status: str
    checkout_mode: str
    provider_data: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class RefundResult:
    provider_refund_id: str
    status: str  # one of PAYMENT_STATUSES (REFUNDED / REFUND_PENDING / REFUND_FAILED)
    amount_minor: int


@dataclass
class ProviderWebhookEvent:
    """A verified, parsed, and normalised webhook event - the payload has
    already passed signature verification, and its provider-native event
    type/status have already been mapped to CoMaz's own vocabulary, by the
    time domain code sees this. app/payments/service.py dispatches purely on
    ``kind`` and reads ``status``/``failure_message`` - it never inspects
    ``raw`` for logic, only for audit storage.
    """

    event_id: str
    kind: str  # one of the WEBHOOK_* constants above
    provider_payment_id: str | None
    provider_refund_id: str | None
    status: str | None  # one of PAYMENT_STATUSES, when kind implies one
    failure_message: str | None
    raw: dict
    # The provider account which emitted the event when the provider supports
    # connected accounts (Stripe's top-level event.account).  Domain code uses
    # this as a second guard in addition to the signed webhook: a Direct
    # Charge event must only ever mutate a payment created on that same
    # connected account.
    provider_account_id: str | None = None
    # Populated only for kind == WEBHOOK_ACCOUNT_UPDATED - the connected
    # account's own normalised status, never a raw provider object:
    # {"account_id": str, "charges_enabled": bool, "payouts_enabled": bool,
    # "details_submitted": bool}. See app/payments/connect.py.
    account: dict | None = None


class WebhookVerificationError(Exception):
    """The webhook payload's signature didn't verify - reject with 400,
    never process the body."""


class PaymentProviderError(RuntimeError):
    """A provider call failed (network, declined, invalid request, ...).
    Domain code catches this at the boundary rather than a provider SDK's
    own exception types, so app/payments/service.py never needs to know
    which SDK raised what."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class ProviderNotConfigured(PaymentProviderError):
    """The selected provider has no working credentials yet (e.g. a PayPal/
    Square skeleton adapter with no real integration behind it). Distinct
    from a mid-call failure: this is refused before any network call is
    attempted."""


class UnsupportedCapability(PaymentProviderError):
    """The selected provider doesn't support an operation the caller asked
    for (see Capabilities.require)."""


class PaymentProvider(ABC):
    """One payment provider adapter. All amounts are minor units (int); all
    currency codes are ISO 4217 upper-case strings (e.g. "GBP")."""

    name: str
    capabilities: Capabilities

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this adapter has everything it needs (credentials, etc.)
        to actually be used right now - checked before any call below is
        attempted. A skeleton adapter (PayPal/Square today) always returns
        False until it's given a real implementation."""

    @abstractmethod
    def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        metadata: dict,
    ) -> PaymentSessionResult:
        """Create a new payment session for a single deposit attempt.
        ``idempotency_key`` must make a retried call with the same key
        return the original session rather than creating a duplicate charge
        (see app/payments/service.py::create_deposit_hold)."""

    @abstractmethod
    def get_payment_status(self, provider_payment_id: str) -> PaymentSessionResult:
        """Fetch the current state of a previously created session - used to
        reconcile a status poll or recover from a missed webhook."""

    @abstractmethod
    def cancel_payment(self, provider_payment_id: str) -> None:
        """Cancel a not-yet-completed session - called when a payment hold
        expires unpaid (app/payments/service.py::expire_stale_payment_holds).
        Must be a safe no-op if the session is already terminal."""

    @abstractmethod
    def refund_payment(
        self, provider_payment_id: str, *, amount_minor: int, idempotency_key: str
    ) -> RefundResult:
        """Refund a succeeded payment (full amount only, today - see
        app/payments/service.py). ``idempotency_key`` prevents a retried
        call from issuing a second refund."""

    @abstractmethod
    def verify_webhook(
        self, payload: bytes, headers: dict, *, webhook_secret: str | None = None
    ) -> ProviderWebhookEvent:
        """Verify the signature on a raw webhook request body, parse it, and
        normalise it into a :class:`ProviderWebhookEvent`. Raises
        WebhookVerificationError on a bad/missing signature - the caller
        must reject with 400 and process nothing.

        ``webhook_secret`` overrides whatever secret the adapter would
        otherwise use from config - needed because Stripe Connect events
        arrive on a *different* webhook endpoint (and secret) from ordinary
        platform events; every other adapter ignores it."""
