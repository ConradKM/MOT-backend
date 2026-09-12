"""The provider-agnostic payment interface.

Every payment provider adapter (app/payments/providers/stripe_provider.py,
.../fake.py, and any future one) implements this ABC. Domain code
(app/payments/service.py, routes, webhooks) depends only on these types -
never on a provider SDK's own request/response shapes - so swapping or
adding a provider never touches booking/webhook logic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderPaymentIntent:
    """What a provider hands back after creating a payment intent/session.

    ``client_secret`` (or provider-equivalent client token) is the *only*
    thing safe to send to the browser - it authorizes the frontend's
    provider-hosted payment element to collect card details directly with
    the provider, never through our backend. Everything else here is safe,
    non-sensitive metadata for our own records.
    """

    provider_payment_id: str
    client_secret: str | None
    status: str  # provider-native status string, mapped by the caller
    metadata: dict = field(default_factory=dict)


@dataclass
class ProviderRefund:
    provider_refund_id: str
    status: str  # provider-native status string, mapped by the caller
    amount_minor: int


@dataclass
class ProviderWebhookEvent:
    """A verified, parsed webhook event - the payload has already passed
    signature verification by the time domain code sees this.

    ``status``/``failure_message`` are pulled out of the provider's own
    payload shape *by the adapter* (see stripe_provider.py) so
    app/payments/service.py never has to know each provider's raw event
    structure - it only ever reads these normalised fields plus
    ``event_type`` to decide what happened.
    """

    event_id: str
    event_type: str  # provider-native type string, e.g. "payment_intent.succeeded"
    provider_payment_id: str | None
    provider_refund_id: str | None
    status: str | None  # the payment intent/refund's own status string
    failure_message: str | None
    raw: dict


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


class PaymentProvider(ABC):
    """One payment provider adapter. All amounts are minor units (int); all
    currency codes are ISO 4217 upper-case strings (e.g. "GBP")."""

    name: str

    @abstractmethod
    def create_payment_intent(
        self,
        *,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        metadata: dict,
    ) -> ProviderPaymentIntent:
        """Create a new payment intent/session for a single deposit
        attempt. ``idempotency_key`` must make a retried call with the same
        key return the original intent rather than creating a duplicate
        charge (see app/payments/service.py::create_deposit_hold)."""

    @abstractmethod
    def retrieve_payment(self, provider_payment_id: str) -> ProviderPaymentIntent:
        """Fetch the current state of a previously created intent - used to
        reconcile a status poll or recover from a missed webhook."""

    @abstractmethod
    def cancel_payment(self, provider_payment_id: str) -> None:
        """Cancel a not-yet-completed intent - called when a payment hold
        expires unpaid (app/payments/service.py::expire_stale_payment_holds).
        Must be a safe no-op if the intent is already terminal."""

    @abstractmethod
    def refund_payment(
        self, provider_payment_id: str, *, amount_minor: int, idempotency_key: str
    ) -> ProviderRefund:
        """Refund a succeeded payment (full amount only - see
        app/payments/service.py). ``idempotency_key`` prevents a retried
        call from issuing a second refund."""

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict) -> ProviderWebhookEvent:
        """Verify the signature on a raw webhook request body and return the
        parsed event. Raises WebhookVerificationError on a bad/missing
        signature - the caller must reject with 400 and process nothing."""
