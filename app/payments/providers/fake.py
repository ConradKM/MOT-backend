"""In-process fake provider - what PAYMENTS_PROVIDER=fake uses (the test
suite's default, see app/config.py::TestConfig). No network calls, no real
account. Lets the whole deposit flow (session creation, webhook processing,
refunds, idempotency) be exercised in CI without a Stripe test account.

Not a mock of any real SDK's shapes - a minimal, independent, in-memory
implementation of the same ``PaymentProvider`` interface every real adapter
implements, including its own (Stripe-shaped, but that's incidental) webhook
event-type vocabulary and its own normalisation into CoMaz's ``WEBHOOK_*``
kinds - so tests exercise the real normalisation boundary, not a shortcut
around it.
"""

import json
import uuid
from typing import ClassVar

from .base import (
    CHECKOUT_MODE_EMBEDDED,
    WEBHOOK_PAYMENT_CANCELLED,
    WEBHOOK_PAYMENT_FAILED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    WEBHOOK_REFUND_UPDATED,
    WEBHOOK_UNHANDLED,
    Capabilities,
    PaymentProvider,
    PaymentProviderError,
    PaymentSessionResult,
    ProviderWebhookEvent,
    RefundResult,
    WebhookVerificationError,
)

# This fake's own made-up "wire" event-type vocabulary, deliberately
# Stripe-shaped (dotted, past-tense-ish) so it looks like a plausible third
# adapter rather than a special case - mapped to CoMaz's normalised kinds
# exactly like a real adapter would map its own provider's names.
_EVENT_KIND_MAP = {
    "payment.succeeded": WEBHOOK_PAYMENT_SUCCEEDED,
    "payment.failed": WEBHOOK_PAYMENT_FAILED,
    "payment.cancelled": WEBHOOK_PAYMENT_CANCELLED,
    "refund.updated": WEBHOOK_REFUND_UPDATED,
}


class FakePaymentProvider(PaymentProvider):
    name = "fake"
    capabilities = Capabilities(
        supports_embedded_checkout=True,
        supports_refunds=True,
        supports_payment_cancellation=True,
        supports_webhooks=True,
        supports_idempotency=True,
    )

    # Class-level so it survives across FakePaymentProvider() instances
    # created per-request within one test process; tests call reset()
    # (typically via a fixture) to start from empty.
    _sessions: ClassVar[dict[str, dict]] = {}

    @classmethod
    def reset(cls) -> None:
        cls._sessions.clear()

    def is_configured(self) -> bool:
        return True

    def create_payment(self, *, amount_minor, currency, idempotency_key, metadata):
        for pid, data in self._sessions.items():
            if data["idempotency_key"] == idempotency_key:
                return self._to_result(pid, data)

        pid = f"fake_pay_{uuid.uuid4().hex[:20]}"
        secret = f"{pid}_secret_{uuid.uuid4().hex[:8]}"
        self._sessions[pid] = {
            "amount_minor": amount_minor,
            "currency": currency,
            "status": "REQUIRES_PAYMENT",
            "idempotency_key": idempotency_key,
            "metadata": dict(metadata),
            "client_secret": secret,
            "refunded_minor": 0,
        }
        return self._to_result(pid, self._sessions[pid])

    def get_payment_status(self, provider_payment_id):
        data = self._sessions.get(provider_payment_id)
        if data is None:
            raise PaymentProviderError("No such fake payment session.", code="not_found")
        return self._to_result(provider_payment_id, data)

    def cancel_payment(self, provider_payment_id):
        data = self._sessions.get(provider_payment_id)
        if data is None or data["status"] in ("SUCCEEDED", "CANCELLED"):
            return
        data["status"] = "CANCELLED"

    def refund_payment(self, provider_payment_id, *, amount_minor, idempotency_key):
        data = self._sessions.get(provider_payment_id)
        if data is None:
            raise PaymentProviderError("No such fake payment session.", code="not_found")
        rid = f"fake_re_{uuid.uuid4().hex[:20]}"
        data["refunded_minor"] += amount_minor
        return RefundResult(provider_refund_id=rid, status="REFUNDED", amount_minor=amount_minor)

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
            kind=_EVENT_KIND_MAP.get(raw.get("type", ""), WEBHOOK_UNHANDLED),
            provider_payment_id=raw.get("provider_payment_id"),
            provider_refund_id=raw.get("provider_refund_id"),
            status=raw.get("status"),
            failure_message=raw.get("failure_message"),
            raw=raw,
        )

    def _to_result(self, provider_payment_id: str, data: dict) -> PaymentSessionResult:
        return PaymentSessionResult(
            provider_payment_id=provider_payment_id,
            status=data["status"],
            checkout_mode=CHECKOUT_MODE_EMBEDDED,
            provider_data={"client_secret": data["client_secret"]},
            metadata=dict(data["metadata"]),
        )
