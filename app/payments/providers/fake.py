"""In-process fake provider - what PAYMENTS_PROVIDER=fake uses (the test
suite's default, see app/config.py::TestConfig). No network calls, no real
account. Lets the whole deposit flow (intent creation, webhook processing,
refunds, idempotency) be exercised in CI without a Stripe test account.

Not a mock of the Stripe SDK's shapes - a minimal, independent, in-memory
implementation of the same ``PaymentProvider`` interface every real adapter
implements, so tests exercise the real abstraction boundary.
"""

import json
import uuid
from typing import ClassVar

from .base import (
    PaymentProvider,
    PaymentProviderError,
    ProviderPaymentIntent,
    ProviderRefund,
    ProviderWebhookEvent,
    WebhookVerificationError,
)


class FakePaymentProvider(PaymentProvider):
    name = "fake"

    # Class-level so it survives across FakePaymentProvider() instances
    # created per-request within one test process; tests call reset()
    # (typically via a fixture) to start from empty.
    _intents: ClassVar[dict[str, dict]] = {}

    @classmethod
    def reset(cls) -> None:
        cls._intents.clear()

    def create_payment_intent(self, *, amount_minor, currency, idempotency_key, metadata):
        for pid, data in self._intents.items():
            if data["idempotency_key"] == idempotency_key:
                return ProviderPaymentIntent(
                    provider_payment_id=pid,
                    client_secret=data["client_secret"],
                    status=data["status"],
                    metadata=data["metadata"],
                )

        pid = f"fake_pi_{uuid.uuid4().hex[:20]}"
        secret = f"{pid}_secret_{uuid.uuid4().hex[:8]}"
        self._intents[pid] = {
            "amount_minor": amount_minor,
            "currency": currency,
            "status": "requires_payment_method",
            "idempotency_key": idempotency_key,
            "metadata": dict(metadata),
            "client_secret": secret,
            "refunded_minor": 0,
        }
        return ProviderPaymentIntent(
            provider_payment_id=pid,
            client_secret=secret,
            status="requires_payment_method",
            metadata=dict(metadata),
        )

    def retrieve_payment(self, provider_payment_id):
        data = self._intents.get(provider_payment_id)
        if data is None:
            raise PaymentProviderError("No such fake payment intent.", code="not_found")
        return ProviderPaymentIntent(
            provider_payment_id=provider_payment_id,
            client_secret=data["client_secret"],
            status=data["status"],
            metadata=dict(data["metadata"]),
        )

    def cancel_payment(self, provider_payment_id):
        data = self._intents.get(provider_payment_id)
        if data is None or data["status"] in ("succeeded", "canceled"):
            return
        data["status"] = "canceled"

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        data = self._intents.get(provider_payment_id)
        if data is None:
            raise PaymentProviderError("No such fake payment intent.", code="not_found")
        rid = f"fake_re_{uuid.uuid4().hex[:20]}"
        data["refunded_minor"] += amount_minor
        return ProviderRefund(provider_refund_id=rid, status="succeeded", amount_minor=amount_minor)

    def verify_webhook(self, payload, headers):
        # Real signature verification is meaningless without a real shared
        # secret - this stands in for it so tests can still exercise the
        # "reject an unsigned/invalid webhook" path (see
        # tests/api/test_payments_webhooks.py).
        if headers.get("Fake-Signature") != "valid":
            raise WebhookVerificationError("Invalid fake webhook signature.")
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise WebhookVerificationError("Malformed webhook payload.") from exc

        return ProviderWebhookEvent(
            event_id=raw["id"],
            event_type=raw["type"],
            provider_payment_id=raw.get("provider_payment_id"),
            provider_refund_id=raw.get("provider_refund_id"),
            status=raw.get("status"),
            failure_message=raw.get("failure_message"),
            raw=raw,
        )
