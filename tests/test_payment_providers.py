"""Unit tests for the provider abstraction itself - no Flask app/database
needed except where a provider genuinely requires app.config (Stripe)."""

import pytest

from app.payments.providers import get_provider
from app.payments.providers.base import (
    Capabilities,
    PaymentSessionResult,
    ProviderNotConfigured,
    ProviderWebhookEvent,
    RefundResult,
    UnsupportedCapability,
)
from app.payments.providers.fake import FakePaymentProvider
from app.payments.providers.paypal import PayPalPaymentProvider
from app.payments.providers.square import SquarePaymentProvider
from app.payments.providers.stripe_provider import StripePaymentProvider


def test_capabilities_require_passes_when_supported():
    caps = Capabilities(supports_refunds=True)
    caps.require("supports_refunds")  # does not raise


def test_capabilities_require_raises_when_unsupported():
    caps = Capabilities(supports_refunds=False)
    with pytest.raises(UnsupportedCapability):
        caps.require("supports_refunds")


def test_get_provider_returns_the_right_adapter_by_name():
    assert isinstance(get_provider("fake"), FakePaymentProvider)
    assert isinstance(get_provider("paypal"), PayPalPaymentProvider)
    assert isinstance(get_provider("square"), SquarePaymentProvider)
    assert isinstance(get_provider("stripe"), StripePaymentProvider)


def test_stripe_adapter_reports_unconfigured_without_keys(app):
    # TestConfig never sets real Stripe keys - see app/config.py.
    assert StripePaymentProvider().is_configured() is False


def test_stripe_declares_wallet_support_skeletons_declare_none():
    assert StripePaymentProvider().capabilities.supported_wallets == ("apple_pay", "google_pay")
    assert PayPalPaymentProvider().capabilities.supported_wallets == ()
    assert SquarePaymentProvider().capabilities.supported_wallets == ()
    assert FakePaymentProvider().capabilities.supported_wallets == ()


def test_capabilities_default_to_no_wallets():
    assert Capabilities().supported_wallets == ()


def test_stripe_client_data_reports_available_wallets(app):
    from app.payments.providers.stripe_provider import _client_data

    data = _client_data("secret_123")
    assert data["available_wallets"] == ["apple_pay", "google_pay"]


def test_stripe_metadata_object_is_converted_without_iterating(app):
    """StripeObject metadata requires ``to_dict`` in current stripe-python."""
    from app.payments.providers.stripe_provider import _metadata_dict

    class StripeMetadataLike:
        def __iter__(self):
            raise AssertionError("Stripe metadata must not be iterated")

        def to_dict(self):
            return {"booking_request_id": "request_123"}

    assert _metadata_dict(StripeMetadataLike()) == {"booking_request_id": "request_123"}


def test_stripe_webhook_normalises_sdk_event_objects_before_parsing(app, monkeypatch):
    """Current stripe-python Event objects reject ``.get`` *and* ``dict()``/
    iteration (``TypeError: ... call .to_dict() for a plain dict``) - modelled
    here to match the real SDK's actual current shape, not a plausible-looking
    guess at it. A prior version of this fake only implemented
    ``to_dict_recursive`` (a method the real SDK doesn't have - the real
    recursive helper is private, ``_to_dict_recursive``) and so this test kept
    passing while production crashed on every real Connect webhook delivery -
    see app/payments/providers/stripe_provider.py::verify_webhook.
    """
    from types import SimpleNamespace

    from app.payments.providers import stripe_provider

    payload = {
        "id": "evt_connect_1",
        "type": "payment_intent.succeeded",
        "account": "acct_connected",
        "data": {"object": {"id": "pi_1", "status": "succeeded"}},
    }

    class EventLike:
        def __getitem__(self, key):
            return payload[key]

        def __iter__(self):
            raise TypeError("Event is not iterable or a mapping; call .to_dict() for a plain dict.")

        def get(self, _key):
            raise AssertionError("Stripe Event.get must not be called")

        def to_dict(self):
            return payload

    fake_stripe = SimpleNamespace(
        Webhook=SimpleNamespace(construct_event=lambda *_args: EventLike())
    )
    monkeypatch.setattr(stripe_provider, "_client", lambda: fake_stripe)

    event = StripePaymentProvider().verify_webhook(b"{}", {"Stripe-Signature": "test"})
    assert event.event_id == "evt_connect_1"
    assert event.status == "SUCCEEDED"
    assert event.provider_payment_id == "pi_1"
    assert event.provider_account_id == "acct_connected"


def test_get_provider_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        get_provider("venmo")


def test_fake_provider_is_always_configured():
    assert FakePaymentProvider().is_configured() is True


@pytest.mark.parametrize("adapter_cls", [PayPalPaymentProvider, SquarePaymentProvider])
def test_skeleton_adapters_report_unconfigured(adapter_cls):
    adapter = adapter_cls()
    assert adapter.is_configured() is False


@pytest.mark.parametrize("adapter_cls", [PayPalPaymentProvider, SquarePaymentProvider])
def test_skeleton_adapters_never_fake_a_successful_payment(adapter_cls):
    """Every real operation must fail loudly and clearly rather than
    pretending to work - a skeleton adapter must never let a booking
    believe a deposit was taken."""
    adapter = adapter_cls()

    with pytest.raises(ProviderNotConfigured):
        adapter.create_payment(amount_minor=1000, currency="GBP", idempotency_key="x", metadata={})
    with pytest.raises(ProviderNotConfigured):
        adapter.get_payment_status("whatever")
    with pytest.raises(ProviderNotConfigured):
        adapter.cancel_payment("whatever")
    with pytest.raises(ProviderNotConfigured):
        adapter.refund_payment("whatever", amount_minor=100, idempotency_key="y")
    with pytest.raises(ProviderNotConfigured):
        adapter.verify_webhook(b"{}", {})


@pytest.mark.parametrize(
    "adapter_cls,checkout_mode",
    [(PayPalPaymentProvider, "REDIRECT"), (SquarePaymentProvider, "EMBEDDED")],
)
def test_skeleton_adapters_declare_a_checkout_mode_and_capabilities(adapter_cls, checkout_mode):
    adapter = adapter_cls()
    assert adapter.checkout_mode == checkout_mode
    assert isinstance(adapter.capabilities, Capabilities)
    assert adapter.capabilities.supports_refunds is True


def test_fake_provider_shares_the_normal_deposit_flow():
    """The fake provider is a real (if in-memory) implementation of the same
    interface Stripe/PayPal/Square use - not a special-cased shortcut."""
    FakePaymentProvider.reset()
    provider = FakePaymentProvider()

    session = provider.create_payment(
        amount_minor=2000, currency="GBP", idempotency_key="dep-1", metadata={"x": "y"}
    )
    assert isinstance(session, PaymentSessionResult)
    assert session.status == "REQUIRES_PAYMENT"
    assert session.checkout_mode == "EMBEDDED"
    assert "client_secret" in session.provider_data

    # Idempotent: the same key returns the same session, not a new one.
    again = provider.create_payment(
        amount_minor=2000, currency="GBP", idempotency_key="dep-1", metadata={"x": "y"}
    )
    assert again.provider_payment_id == session.provider_payment_id

    fetched = provider.get_payment_status(session.provider_payment_id)
    assert fetched.status == "REQUIRES_PAYMENT"

    refund = provider.refund_payment(
        session.provider_payment_id, amount_minor=2000, idempotency_key="ref-1"
    )
    assert isinstance(refund, RefundResult)
    assert refund.status == "REFUNDED"

    FakePaymentProvider.reset()


def test_fake_provider_normalises_its_own_webhook_vocabulary():
    import json

    FakePaymentProvider.reset()
    provider = FakePaymentProvider()
    session = provider.create_payment(
        amount_minor=500, currency="GBP", idempotency_key="dep-2", metadata={}
    )

    event = provider.verify_webhook(
        json.dumps(
            {
                "id": "evt_1",
                "type": "payment.succeeded",
                "provider_payment_id": session.provider_payment_id,
                "status": "SUCCEEDED",
            }
        ).encode(),
        {"Fake-Signature": "valid"},
    )
    assert isinstance(event, ProviderWebhookEvent)
    assert event.kind == "payment.succeeded"
    assert event.status == "SUCCEEDED"

    FakePaymentProvider.reset()
