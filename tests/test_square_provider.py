"""Square adapter contract tests - real adapter + OAuth code against an
in-process Square API (tests/square_helpers.py), no network or account."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.payments import square_oauth
from app.payments.providers import get_provider
from app.payments.providers.base import (
    WEBHOOK_ACCOUNT_REVOKED,
    WEBHOOK_PAYMENT_CANCELLED,
    WEBHOOK_PAYMENT_FAILED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    WEBHOOK_REFUND_UPDATED,
    WEBHOOK_UNHANDLED,
    PaymentProviderError,
    ProviderNotConfigured,
    UnsupportedCapability,
    WebhookVerificationError,
)
from app.payments.providers.square import (
    IDEMPOTENCY_KEY_MAX,
    SquarePaymentProvider,
    _map_payment_status,
    _map_refund_status,
    square_idempotency_key,
)
from app.payments.secrets import (
    PaymentsSecretsNotConfiguredError,
    decrypt_token,
    encrypt_token,
    payments_secrets_configured,
)
from tests import square_helpers as sq


@pytest.fixture()
def fake_square(app):
    fake = sq.install(app)
    yield fake
    sq.uninstall(app)


@pytest.fixture()
def connected(session, garage, fake_square):
    return sq.connect_garage(session, garage, fake_square)


def _provider(fake):
    return get_provider("square", connected_account_id=fake.merchant_id)


# --- status mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "square_status,expected",
    [
        ("APPROVED", "PENDING"),
        ("PENDING", "PENDING"),
        ("COMPLETED", "SUCCEEDED"),
        ("CANCELED", "CANCELLED"),
        ("FAILED", "FAILED"),
        (None, "PENDING"),
        ("SOMETHING_NEW", "PENDING"),
    ],
)
def test_every_square_payment_status_maps_to_a_normalised_one(square_status, expected):
    assert _map_payment_status(square_status) == expected


@pytest.mark.parametrize(
    "square_status,expected",
    [
        ("PENDING", "REFUND_PENDING"),
        ("COMPLETED", "REFUNDED"),
        ("REJECTED", "REFUND_FAILED"),
        ("FAILED", "REFUND_FAILED"),
        (None, "REFUND_PENDING"),
    ],
)
def test_every_square_refund_status_maps_to_a_normalised_one(square_status, expected):
    assert _map_refund_status(square_status) == expected


# --- idempotency keys -------------------------------------------------------


def test_service_idempotency_keys_fit_square_unchanged():
    import uuid

    deposit_key = f"deposit-{uuid.uuid4()}"
    refund_key = f"refund-{uuid.uuid4()}"
    assert len(deposit_key) <= IDEMPOTENCY_KEY_MAX
    assert square_idempotency_key(deposit_key) == deposit_key
    assert square_idempotency_key(refund_key) == refund_key


def test_over_long_keys_become_a_stable_45_char_digest():
    long_key = "x" * 100
    first = square_idempotency_key(long_key)
    assert len(first) == IDEMPOTENCY_KEY_MAX
    assert square_idempotency_key(long_key) == first
    assert square_idempotency_key("y" * 100) != first


# --- capabilities / phase one -----------------------------------------------


def test_capabilities_are_honest():
    caps = SquarePaymentProvider.capabilities
    assert caps.requires_client_token_submission is True
    assert caps.supports_partial_refunds is True
    assert caps.supported_wallets == ()
    assert caps.supports_payment_cancellation is False


def test_stripe_and_fake_do_not_take_client_tokens(app):
    assert get_provider("stripe").capabilities.requires_client_token_submission is False
    with pytest.raises(UnsupportedCapability):
        get_provider("fake").charge_with_source(
            amount_minor=1,
            currency="GBP",
            source_id="x",
            verification_token=None,
            idempotency_key="k",
        )


def test_is_configured_requires_every_application_setting(app, fake_square):
    assert SquarePaymentProvider().is_configured() is True
    app.config["SQUARE_WEBHOOK_NOTIFICATION_URL"] = ""
    assert SquarePaymentProvider().is_configured() is False


def test_create_payment_charges_nothing_and_returns_only_client_safe_data(connected, fake_square):
    session = _provider(fake_square).create_payment(
        amount_minor=2000, currency="gbp", idempotency_key="deposit-x", metadata={}
    )
    assert session.provider_payment_id is None
    assert session.status == "REQUIRES_PAYMENT"
    assert session.checkout_mode == "EMBEDDED"
    assert session.provider_data == {
        "application_id": sq.APPLICATION_ID,
        "location_id": "LOC_GB_1",
        "environment": "sandbox",
        "amount_minor": 2000,
        "amount": "20.00",
        "currency": "GBP",
    }
    assert fake_square.requests == []  # no Square call at all


def test_create_payment_without_a_location_is_refused(session, garage, fake_square):
    sq.connect_garage(session, garage, fake_square, location_id=None)
    with pytest.raises(ProviderNotConfigured):
        _provider(fake_square).create_payment(
            amount_minor=2000, currency="GBP", idempotency_key="k", metadata={}
        )


# --- phase two / payments ---------------------------------------------------


def test_charge_with_source_creates_a_payment_with_idempotency_and_sca(connected, fake_square):
    provider = _provider(fake_square)
    result = provider.charge_with_source(
        amount_minor=2000,
        currency="GBP",
        source_id="cnon:card-nonce-ok",
        verification_token="verf:token",
        idempotency_key="deposit-11111111-2222-3333-4444-555555555555",
        reference="ABC123",
    )
    assert result.status == "SUCCEEDED"
    assert result.provider_payment_id in fake_square.payments
    (call,) = fake_square.calls("POST", "/v2/payments")
    body = json.loads(call.content)
    assert body["idempotency_key"] == "deposit-11111111-2222-3333-4444-555555555555"
    assert body["verification_token"] == "verf:token"
    assert body["location_id"] == "LOC_GB_1"
    assert body["amount_money"] == {"amount": 2000, "currency": "GBP"}
    assert body["autocomplete"] is True
    assert call.headers["Square-Version"] == "2026-08-19"

    # A retried identical call returns the same payment, never a second one.
    again = provider.charge_with_source(
        amount_minor=2000,
        currency="GBP",
        source_id="cnon:card-nonce-ok",
        verification_token="verf:token",
        idempotency_key="deposit-11111111-2222-3333-4444-555555555555",
        reference="ABC123",
    )
    assert again.provider_payment_id == result.provider_payment_id
    assert len(fake_square.payments) == 1


def test_a_card_decline_becomes_a_customer_safe_provider_error(connected, fake_square):
    fake_square.decline_next = True
    with pytest.raises(PaymentProviderError) as info:
        _provider(fake_square).charge_with_source(
            amount_minor=2000,
            currency="GBP",
            source_id="cnon:declined",
            verification_token=None,
            idempotency_key="k1",
        )
    assert str(info.value) == "Your card was declined."
    assert info.value.code == "card_declined"


def test_an_unknown_square_error_gets_a_generic_message(connected, fake_square):
    with pytest.raises(PaymentProviderError) as info:
        _provider(fake_square).get_payment_status("does-not-exist")
    assert "detail" not in str(info.value)  # Square's raw detail never leaks


def test_a_401_triggers_one_refresh_and_retry(connected, fake_square):
    fake_square.unauthorized_next = True
    result = _provider(fake_square).charge_with_source(
        amount_minor=500,
        currency="GBP",
        source_id="cnon:ok",
        verification_token=None,
        idempotency_key="k2",
    )
    assert result.status == "SUCCEEDED"
    assert fake_square.refresh_count == 1


def test_get_status_and_cancel_are_safe_on_terminal_payments(connected, fake_square):
    provider = _provider(fake_square)
    charged = provider.charge_with_source(
        amount_minor=500,
        currency="GBP",
        source_id="cnon:ok",
        verification_token=None,
        idempotency_key="k3",
    )
    assert provider.get_payment_status(charged.provider_payment_id).status == "SUCCEEDED"
    provider.cancel_payment(charged.provider_payment_id)  # COMPLETED: no-op
    assert fake_square.calls("POST", f"/v2/payments/{charged.provider_payment_id}/cancel") == []
    provider.cancel_payment(None)  # nothing was ever charged: no-op


def test_cancel_cancels_an_approved_payment(connected, fake_square):
    fake_square.payment_status_on_create = "APPROVED"
    provider = _provider(fake_square)
    charged = provider.charge_with_source(
        amount_minor=500,
        currency="GBP",
        source_id="cnon:ok",
        verification_token=None,
        idempotency_key="k4",
    )
    assert charged.status == "PENDING"
    provider.cancel_payment(charged.provider_payment_id)
    assert fake_square.payments[charged.provider_payment_id]["status"] == "CANCELED"


@pytest.mark.parametrize("amount", [2000, 750])
def test_full_and_partial_refunds(connected, fake_square, amount):
    provider = _provider(fake_square)
    charged = provider.charge_with_source(
        amount_minor=2000,
        currency="GBP",
        source_id="cnon:ok",
        verification_token=None,
        idempotency_key="k5",
    )
    refund = provider.refund_payment(
        charged.provider_payment_id, amount_minor=amount, idempotency_key="refund-k5"
    )
    assert refund.status == "REFUND_PENDING"
    assert refund.amount_minor == amount
    again = provider.refund_payment(
        charged.provider_payment_id, amount_minor=amount, idempotency_key="refund-k5"
    )
    assert again.provider_refund_id == refund.provider_refund_id
    assert len(fake_square.refunds) == 1


def test_calls_for_a_merchant_that_is_not_connected_are_refused(app, fake_square):
    with pytest.raises(ProviderNotConfigured):
        get_provider("square", connected_account_id="UNKNOWN").get_payment_status("p")


# --- webhook verification ---------------------------------------------------


def _signed(body, **kwargs):
    return {"x-square-hmacsha256-signature": sq.sign(body, **kwargs)}


def test_a_valid_signature_verifies(connected, fake_square):
    body = sq.webhook_body(
        "payment.updated",
        {"id": "p1", "status": "COMPLETED"},
        merchant_id=fake_square.merchant_id,
        event_id="e1",
    )
    event = SquarePaymentProvider().verify_webhook(body, _signed(body))
    assert event.event_id == "e1"
    assert event.kind == WEBHOOK_PAYMENT_SUCCEEDED
    assert event.provider_payment_id == "p1"
    assert event.provider_account_id == fake_square.merchant_id


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: (body + b" ", _signed(body)),  # tampered body
        lambda body: (body, _signed(body, url="https://evil.test/hook")),  # wrong URL
        lambda body: (body, _signed(body, key="wrong-key")),  # wrong key
        lambda body: (body, {}),  # missing header
        lambda body: (body, {"x-square-hmacsha256-signature": "not-base64!"}),
    ],
)
def test_invalid_signatures_are_rejected(connected, fake_square, mutate):
    body = sq.webhook_body(
        "payment.updated", {"id": "p1", "status": "COMPLETED"}, merchant_id=fake_square.merchant_id
    )
    payload, headers = mutate(body)
    with pytest.raises(WebhookVerificationError):
        SquarePaymentProvider().verify_webhook(payload, headers)


def test_verification_refuses_when_unconfigured(app):
    with pytest.raises(WebhookVerificationError):
        SquarePaymentProvider().verify_webhook(b"{}", {"x-square-hmacsha256-signature": "x"})


@pytest.mark.parametrize(
    "event_type,obj,kind,status",
    [
        (
            "payment.updated",
            {"id": "p", "status": "COMPLETED"},
            WEBHOOK_PAYMENT_SUCCEEDED,
            "SUCCEEDED",
        ),
        ("payment.created", {"id": "p", "status": "FAILED"}, WEBHOOK_PAYMENT_FAILED, "FAILED"),
        (
            "payment.updated",
            {"id": "p", "status": "CANCELED"},
            WEBHOOK_PAYMENT_CANCELLED,
            "CANCELLED",
        ),
        ("payment.updated", {"id": "p", "status": "APPROVED"}, WEBHOOK_UNHANDLED, "PENDING"),
        (
            "refund.updated",
            {"id": "r", "payment_id": "p", "status": "COMPLETED"},
            WEBHOOK_REFUND_UPDATED,
            "REFUNDED",
        ),
        (
            "refund.created",
            {"id": "r", "payment_id": "p", "status": "PENDING"},
            WEBHOOK_REFUND_UPDATED,
            "REFUND_PENDING",
        ),
        ("oauth.authorization.revoked", {"revoked_at": "now"}, WEBHOOK_ACCOUNT_REVOKED, None),
        ("customer.created", {"id": "c"}, WEBHOOK_UNHANDLED, None),
    ],
)
def test_event_kinds_are_normalised(connected, fake_square, event_type, obj, kind, status):
    body = sq.webhook_body(event_type, obj, merchant_id=fake_square.merchant_id)
    event = SquarePaymentProvider().verify_webhook(body, _signed(body))
    assert event.kind == kind
    assert event.status == status


def test_an_unknown_merchant_is_unhandled(connected, fake_square):
    body = sq.webhook_body(
        "payment.updated", {"id": "p", "status": "COMPLETED"}, merchant_id="SOMEONE_ELSE"
    )
    event = SquarePaymentProvider().verify_webhook(body, _signed(body))
    assert event.kind == WEBHOOK_UNHANDLED


# --- token encryption -------------------------------------------------------


def test_token_encryption_round_trips_and_never_stores_plaintext(app):
    ciphertext = encrypt_token("EAAA-secret")
    assert "EAAA-secret" not in ciphertext
    assert decrypt_token(ciphertext) == "EAAA-secret"


def test_payments_key_is_separate_from_the_comms_key(app):
    from app.communications.secrets import SecretDecryptionError, decrypt_secret

    assert app.config["PAYMENTS_SECRET_KEY"] != app.config["COMMS_SECRET_KEY"]
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(encrypt_token("EAAA-secret"))


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
def test_no_usable_key_blocks_storing_tokens(app, key):
    original = app.config["PAYMENTS_SECRET_KEY"]
    app.config["PAYMENTS_SECRET_KEY"] = key
    try:
        assert payments_secrets_configured() is False
        with pytest.raises(PaymentsSecretsNotConfiguredError):
            encrypt_token("EAAA-secret")
    finally:
        app.config["PAYMENTS_SECRET_KEY"] = original


# --- lazy refresh -----------------------------------------------------------


def test_a_fresh_token_is_used_without_refreshing(connected, fake_square):
    square_oauth.access_token_for_merchant(fake_square.merchant_id)
    assert fake_square.refresh_count == 0


def test_a_token_near_expiry_is_refreshed_once(session, garage, fake_square):
    settings = sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) + timedelta(days=2)
    )
    old_ciphertext = settings.square_access_token_enc
    token, _ = square_oauth.access_token_for_merchant(fake_square.merchant_id)
    db.session.commit()
    assert fake_square.refresh_count == 1
    session.refresh(settings)
    assert settings.square_access_token_enc != old_ciphertext
    assert settings.square_token_expires_at > datetime.now(UTC) + timedelta(days=20)
    assert decrypt_token(settings.square_access_token_enc) == token
    # The second caller sees the new expiry and doesn't refresh again.
    square_oauth.access_token_for_merchant(fake_square.merchant_id)
    assert fake_square.refresh_count == 1


def test_concurrent_callers_refresh_only_once(app, session, garage, fake_square):
    sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    original = fake_square._token

    def slow_token(body):  # widen the race window
        time.sleep(0.3)
        return original(body)

    fake_square._token = slow_token
    errors = []

    def worker():
        with app.app_context():
            try:
                square_oauth.access_token_for_merchant(fake_square.merchant_id)
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 - any failure is surfaced below
                errors.append(exc)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert fake_square.refresh_count == 1


def test_a_revoked_refresh_marks_the_business_revoked(session, garage, fake_square):
    settings = sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    fake_square.refresh_result = "revoked"
    with pytest.raises(square_oauth.SquareNotConnected):
        square_oauth.access_token_for_merchant(fake_square.merchant_id)
    db.session.commit()
    session.refresh(settings)
    assert settings.square_status == "REVOKED"
    from app.payments.config import is_payments_configured

    assert is_payments_configured("square", garage) is False


def test_a_refresh_hiccup_keeps_using_a_still_valid_token(session, garage, fake_square):
    settings = sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) + timedelta(days=2)
    )
    fake_square.refresh_result = "error"
    square_oauth.access_token_for_merchant(fake_square.merchant_id)
    session.refresh(settings)
    assert settings.square_status == "CONNECTED"


def test_a_failed_refresh_of_an_expired_token_marks_error(session, garage, fake_square):
    settings = sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    fake_square.refresh_result = "error"
    with pytest.raises(square_oauth.SquareNotConnected):
        square_oauth.access_token_for_merchant(fake_square.merchant_id)
    session.refresh(settings)
    assert settings.square_status == "ERROR"


def test_refresh_cli_catches_stragglers(app, session, garage, fake_square):
    sq.connect_garage(
        session, garage, fake_square, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    result = app.test_cli_runner().invoke(args=["refresh-square-tokens"])
    assert result.exit_code == 0, result.output
    assert "refreshed" in result.output
    assert fake_square.refresh_count == 1
    assert "EAAA" not in result.output


def test_readiness_requires_location_and_a_usable_token(session, garage, fake_square):
    settings = sq.connect_garage(session, garage, fake_square)
    assert square_oauth.square_account_ready(settings) is True
    settings.square_location_id = None
    assert square_oauth.square_account_ready(settings) is False
    settings.square_location_id = "LOC_GB_1"
    settings.square_token_expires_at = datetime.now(UTC) - timedelta(days=1)
    settings.square_refresh_token_enc = None
    assert square_oauth.square_account_ready(settings) is False
    assert (
        square_oauth.square_account_ready(GaragePaymentSettings(square_status="NOT_CONNECTED"))
        is False
    )
