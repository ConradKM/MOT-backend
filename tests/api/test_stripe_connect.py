"""Stripe Connect seams: tenant ownership, readiness, and Direct Charges.

These tests intentionally use small Stripe/provider fakes.  They exercise
CoMaz's boundary and never need a Stripe account or network access.
"""

import datetime

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.models.payments.payment import BookingPayment
from app.models.payments.webhook_event import PaymentWebhookEvent
from app.payments.connect import (
    create_account_link,
    create_connected_account,
    refresh_connect_status,
)
from app.payments.providers.base import (
    WEBHOOK_ACCOUNT_UPDATED,
    WEBHOOK_PAYMENT_SUCCEEDED,
    PaymentSessionResult,
    ProviderWebhookEvent,
)
from app.payments.service import process_webhook, refund_deposit


def _deposit_type(session, garage):
    item = GarageAppointmentType(
        garage_id=garage.id,
        name="Connect MOT",
        status="ACTIVE",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    session.add(item)
    session.commit()
    return item


def _future_weekday():
    day = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    return day.isoformat()


def _payload(item):
    return {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "CN11 ECT",
        "preferred_date": _future_weekday(),
        "preferred_time": "09:30:00",
        "appointment_type_id": str(item.id),
    }


class _AccountApi:
    def __init__(self):
        self.created = []
        self.record = {
            "id": "acct_connect_a",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
        }

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("Account", (), {"id": "acct_connect_a"})()

    def retrieve(self, account_id):
        assert account_id == "acct_connect_a"
        return self.record


class _FakeV2Accounts:
    """Fake for ``stripe.StripeClient(...).v2.core.accounts`` - the client
    create_connected_account now uses for new-account creation (Accounts
    v2). Every other Connect call in the module keeps using the classic
    ``stripe.<Resource>`` pattern faked by ``_AccountApi`` etc."""

    def __init__(self, account_id="acct_connect_a"):
        self.account_id = account_id
        self.created = []

    def create(self, params):
        self.created.append(params)
        return type("V2Account", (), {"id": self.account_id})()


class _FakeV2AccountLinks:
    def __init__(self):
        self.created = []

    def create(self, params):
        self.created.append(params)
        return type("V2AccountLink", (), {"url": "https://connect.stripe.test/onboarding"})()


def _patch_v2_create(monkeypatch, account_id="acct_connect_a"):
    """Patch ``app.payments.connect._v2_client`` so
    ``create_connected_account`` succeeds without a real Stripe account -
    returns the ``_FakeV2Accounts`` recorder to assert on."""
    accounts = _FakeV2Accounts(account_id)
    links = _FakeV2AccountLinks()
    v2_client = type(
        "V2Client",
        (),
        {
            "v2": type(
                "V2",
                (),
                {"core": type("Core", (), {"accounts": accounts, "account_links": links})()},
            )()
        },
    )()
    monkeypatch.setattr("app.payments.connect._v2_client", lambda: v2_client)
    return accounts, links


class _PaymentMethodDomainApi:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("PaymentMethodDomain", (), {"id": "pmd_1"})()


def test_connect_account_creation_and_status_refresh(session, garage, monkeypatch):
    api = _AccountApi()
    domains = _PaymentMethodDomainApi()
    stripe = type("Stripe", (), {"Account": api, "PaymentMethodDomain": domains})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)
    v2_accounts, _ = _patch_v2_create(monkeypatch)

    assert create_connected_account(garage) == "acct_connect_a"
    assert create_connected_account(garage) == "acct_connect_a"  # idempotent
    # Only the FIRST call actually reaches Stripe - idempotent on the
    # already-stored account id, exactly like the old v1 flow.
    assert len(v2_accounts.created) == 1
    assert v2_accounts.created[0]["contact_email"] == garage.email
    # Stripe Accounts v2 rejects `express` with Stripe collecting both fees
    # and losses (account_controller_unsupported_configuration).  CoMaz is a
    # Direct Charges SaaS platform, so the business gets the full Stripe
    # Dashboard and Stripe remains responsible for fees/losses.
    assert v2_accounts.created[0]["dashboard"] == "full"
    assert v2_accounts.created[0]["defaults"]["responsibilities"] == {
        "fees_collector": "stripe",
        "losses_collector": "stripe",
    }

    settings = refresh_connect_status(garage)
    assert settings.stripe_onboarding_complete is True
    assert settings.stripe_charges_enabled is True
    assert settings.stripe_payouts_enabled is True

    # Apple Pay needs the public booking domain registered per connected
    # account for Direct Charges - see docs/STRIPE_CONNECT_SETUP.md. This
    # used to be a manual Dashboard step; refresh_connect_status now does it
    # automatically once the account can actually take charges.
    assert len(domains.created) == 1
    assert domains.created[0]["stripe_account"] == "acct_connect_a"
    assert domains.created[0]["domain_name"]  # the configured BOOKING_BASE_URL's host


def test_v2_account_link_uses_the_same_v2_lifecycle_and_fresh_urls(session, garage, monkeypatch):
    """Regression for production: a v2 account cannot be onboarded through
    the legacy v1 AccountLink endpoint. Retries retain this garage's account
    but mint a fresh tenant-bound onboarding link."""
    accounts, links = _patch_v2_create(monkeypatch)
    create_connected_account(garage)

    first = create_account_link(
        garage,
        return_url="https://app.comaz.co.uk/a/settings/payments?onboarding=return",
        refresh_url="https://app.comaz.co.uk/a/settings/payments?onboarding=refresh",
    )
    second = create_account_link(
        garage,
        return_url="https://app.comaz.co.uk/a/settings/payments?onboarding=return",
        refresh_url="https://app.comaz.co.uk/a/settings/payments?onboarding=refresh",
    )

    assert first == second == "https://connect.stripe.test/onboarding"
    assert len(accounts.created) == 1
    assert len(links.created) == 2
    assert all(item["account"] == "acct_connect_a" for item in links.created)
    onboarding = links.created[0]["use_case"]["account_onboarding"]
    assert onboarding["configurations"] == ["merchant"]
    assert onboarding["return_url"].endswith("onboarding=return")
    assert onboarding["refresh_url"].endswith("onboarding=refresh")


def test_backfill_cli_registers_the_domain_for_an_already_onboarded_garage(
    app, session, garage, monkeypatch
):
    """A garage that finished Connect onboarding before automatic domain
    registration existed must not need its staff to visit Payments settings
    before Apple Pay can work - `flask backfill-payment-method-domains`
    catches those up in one pass."""
    from app.models.payments.garage_payment_settings import GaragePaymentSettings

    api = _AccountApi()
    domains = _PaymentMethodDomainApi()
    stripe = type("Stripe", (), {"Account": api, "PaymentMethodDomain": domains})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)

    settings = GaragePaymentSettings(garage_id=garage.id, provider="stripe")
    settings.stripe_account_id = "acct_connect_a"
    session.add(settings)
    session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["backfill-payment-method-domains"])

    assert result.exit_code == 0, result.output
    assert len(domains.created) == 1
    assert domains.created[0]["stripe_account"] == "acct_connect_a"
    assert "checked 1 garage" in result.output


def test_get_wallet_domain_status_reports_apple_pay_verification_state(
    session, garage, monkeypatch
):
    """Registering a domain doesn't mean Apple Pay is immediately usable -
    Stripe verifies domain ownership asynchronously. This is the live check
    that lets staff (and this codebase) tell "registered but not verified
    yet" apart from "actually working"."""
    from app.payments.connect import get_wallet_domain_status

    api = _AccountApi()

    class _ListingDomainApi:
        def list(self, **kwargs):
            assert kwargs["stripe_account"] == "acct_connect_a"
            record = {
                "domain_name": "app.comaz.co.uk",
                "enabled": True,
                "apple_pay": {
                    "status": "pending",
                    "status_details": {"error_message": None},
                },
            }
            return {"data": [record]}

    stripe = type("Stripe", (), {"Account": api, "PaymentMethodDomain": _ListingDomainApi()})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)
    _patch_v2_create(monkeypatch)

    create_connected_account(garage)
    refresh_connect_status(garage)

    status = get_wallet_domain_status(garage)
    assert status == {
        "domain": "app.comaz.co.uk",
        "enabled": True,
        "apple_pay_status": "pending",
        "apple_pay_status_details": None,
    }


def test_wallet_domain_status_is_none_before_charges_are_enabled(session, garage):
    from app.payments.connect import get_wallet_domain_status

    assert get_wallet_domain_status(garage) is None


def test_webhook_registers_the_payment_method_domain_once_charges_become_enabled(
    session, garage, monkeypatch
):
    """Apple Pay must not depend on staff ever opening Payments settings -
    a real account.updated webhook (the same trigger that flips
    stripe_charges_enabled) should register the domain on its own."""
    from app.models.payments.garage_payment_settings import GaragePaymentSettings
    from app.payments.connect import sync_account_from_webhook

    settings = GaragePaymentSettings(garage_id=garage.id, provider="stripe")
    settings.stripe_account_id = "acct_webhook_a"
    session.add(settings)
    session.commit()

    domains = _PaymentMethodDomainApi()
    stripe = type("Stripe", (), {"PaymentMethodDomain": domains})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)

    sync_account_from_webhook(
        {
            "account_id": "acct_webhook_a",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
        }
    )

    assert len(domains.created) == 1
    assert domains.created[0]["stripe_account"] == "acct_webhook_a"


def test_status_refresh_survives_domain_registration_failure(session, garage, monkeypatch):
    """A garage's Stripe status must still refresh correctly even if
    registering the Apple Pay domain fails for some reason - that's a wallet
    nicety, never allowed to block onboarding."""
    api = _AccountApi()

    class _FailingDomainApi:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    stripe = type("Stripe", (), {"Account": api, "PaymentMethodDomain": _FailingDomainApi()})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)
    _patch_v2_create(monkeypatch)

    create_connected_account(garage)
    settings = refresh_connect_status(garage)

    assert settings.stripe_onboarding_complete is True
    assert settings.stripe_charges_enabled is True


def test_connect_status_refresh_accepts_stripe_object(session, garage, monkeypatch):
    """The live stripe SDK returns StripeObject, which has no dict ``get``."""
    import stripe

    api = _AccountApi()
    api.record = stripe.Account.construct_from(
        {
            "id": "acct_connect_a",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
        },
        "sk_test_fake",
    )
    monkeypatch.setattr(
        "app.payments.connect._client", lambda: type("Stripe", (), {"Account": api})()
    )
    _patch_v2_create(monkeypatch)

    create_connected_account(garage)
    settings = refresh_connect_status(garage)

    assert settings.stripe_onboarding_complete is True
    assert settings.stripe_charges_enabled is True
    assert settings.stripe_payouts_enabled is True


def test_connect_start_never_leaks_the_raw_stripe_error_to_the_browser(
    authenticated_client, monkeypatch
):
    """Regression guard for a real incident: a raw Stripe API exception
    (request id, doc links and all) was shown verbatim on the Payments
    settings page. The business only ever sees a generic, safe message;
    the real detail belongs in the platform's own logs."""
    from app.payments.connect import ConnectError

    def _boom(garage):
        raise ConnectError(
            "Stripe no longer recommends Accounts v1 for new Connect integrations. "
            "Create connected accounts with POST /v2/core/accounts instead: "
            "https://docs.stripe.com/api/v2/core/accounts (request req_abc123)",
            code="v1_deprecated",
        )

    monkeypatch.setattr("app.payments.routes.create_connected_account", _boom)

    response = authenticated_client.post("/api/payments/stripe/connect")

    assert response.status_code == 503
    body = response.get_json()
    assert (
        body["message"]
        == "We couldn't start Stripe setup. Please try again or contact CoMaz support."
    )
    assert "req_abc123" not in body["message"]
    assert "docs.stripe.com" not in body["message"]


def test_connect_routes_are_owner_only_and_tenant_scoped(
    authenticated_client, second_authenticated_client, client, monkeypatch
):
    monkeypatch.setattr("app.payments.routes.create_connected_account", lambda garage: "acct_owner")
    monkeypatch.setattr(
        "app.payments.routes.create_account_link",
        lambda garage, **kwargs: "https://connect.test/link",
    )

    assert client.post("/api/payments/stripe/connect").status_code == 401
    response = authenticated_client.post("/api/payments/stripe/connect")
    assert response.status_code == 200
    assert response.get_json()["url"] == "https://connect.test/link"
    # A different owner's request resolves only their JWT's garage; no garage
    # id is accepted from the browser to select another tenant.
    response = second_authenticated_client.get("/api/payments/stripe/status")
    assert response.status_code == 200
    assert response.get_json()["stripe_account_id"] is None


def test_connect_readiness_gates_direct_charge_and_snapshots_account(
    app, client, session, garage, monkeypatch
):
    settings = GaragePaymentSettings(
        garage_id=garage.id,
        provider="stripe",
        stripe_account_id="acct_connect_a",
        stripe_charges_enabled=False,
    )
    session.add(settings)
    session.commit()
    item = _deposit_type(session, garage)
    monkeypatch.setitem(app.config, "STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setitem(app.config, "STRIPE_WEBHOOK_SECRET", "whsec_test_fake")

    blocked = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(item)
    )
    assert blocked.status_code == 503

    settings.stripe_charges_enabled = True
    session.commit()
    captured = {}

    class Provider:
        def create_payment(self, **kwargs):
            captured.update(kwargs)
            return PaymentSessionResult(
                provider_payment_id="pi_connect_1",
                status="REQUIRES_PAYMENT",
                checkout_mode="EMBEDDED",
                provider_data={},
                metadata={},
            )

    monkeypatch.setattr("app.payments.service.get_provider_for_garage", lambda _: Provider())
    created = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(item)
    )
    assert created.status_code == 201
    payment = BookingPayment.query.filter_by(provider_payment_id="pi_connect_1").one()
    assert payment.provider_account_id == "acct_connect_a"
    assert captured["amount_minor"] == 2000


def test_connect_account_webhook_and_payment_event_are_account_bound(session, garage):
    settings = GaragePaymentSettings(
        garage_id=garage.id,
        provider="stripe",
        stripe_account_id="acct_connect_a",
    )
    booking = BookingRequest(
        garage_id=garage.id,
        status="AWAITING_PAYMENT",
        booking_reference="BKCONNECT",
        customer_first_name="Alex",
        customer_last_name="Turner",
        customer_email="alex@example.com",
        customer_phone="+447123456789",
        vehicle_registration="CN11ECT",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7),
        preferred_time=datetime.time(9, 30),
    )
    session.add_all([settings, booking])
    session.flush()
    payment = BookingPayment(
        garage_id=garage.id,
        booking_request_id=booking.id,
        provider="stripe",
        provider_account_id="acct_connect_a",
        provider_payment_id="pi_connect_1",
        amount_minor=2000,
        currency="GBP",
        status="REQUIRES_PAYMENT",
    )
    # Provider payment ids are account-scoped inputs for a Connect webhook.
    # Keep a second tenant/account with the same opaque id to prove lookup is
    # filtered by the verified account rather than whichever row happens to
    # sort first in the database.
    other_booking = BookingRequest(
        garage_id=garage.id,
        status="AWAITING_PAYMENT",
        booking_reference="BKCONNECTOTHER",
        customer_first_name="Other",
        customer_last_name="Customer",
        customer_email="other@example.com",
        customer_phone="+447123456788",
        vehicle_registration="CN11OTH",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7),
        preferred_time=datetime.time(10, 30),
    )
    session.add_all([payment, other_booking])
    session.flush()
    other_payment = BookingPayment(
        garage_id=garage.id,
        booking_request_id=other_booking.id,
        provider="stripe",
        provider_account_id="acct_other",
        provider_payment_id="pi_connect_1",
        amount_minor=2000,
        currency="GBP",
        status="REQUIRES_PAYMENT",
    )
    session.add(other_payment)
    session.commit()

    events = iter(
        (
            ProviderWebhookEvent(
                "evt_account",
                WEBHOOK_ACCOUNT_UPDATED,
                None,
                None,
                None,
                None,
                {},
                account={
                    "account_id": "acct_connect_a",
                    "charges_enabled": True,
                    "payouts_enabled": True,
                    "details_submitted": True,
                },
            ),
            ProviderWebhookEvent(
                "evt_wrong_account",
                WEBHOOK_PAYMENT_SUCCEEDED,
                "pi_connect_1",
                None,
                None,
                None,
                {},
                provider_account_id="acct_other",
            ),
            ProviderWebhookEvent(
                "evt_right_account",
                WEBHOOK_PAYMENT_SUCCEEDED,
                "pi_connect_1",
                None,
                None,
                None,
                {},
                provider_account_id="acct_connect_a",
            ),
        )
    )

    class Provider:
        name = "stripe"

        def verify_webhook(self, *_args, **_kwargs):
            return next(events)

    from unittest.mock import patch

    with patch("app.payments.service.get_provider", return_value=Provider()):
        process_webhook("stripe", b"{}", {}, webhook_secret="whsec_connect")
        process_webhook("stripe", b"{}", {}, webhook_secret="whsec_connect")
        process_webhook("stripe", b"{}", {}, webhook_secret="whsec_connect")

    session.refresh(settings)
    session.refresh(payment)
    session.refresh(other_payment)
    assert settings.stripe_charges_enabled is True
    assert payment.status == "SUCCEEDED"
    assert other_payment.status == "SUCCEEDED"


def test_refund_uses_the_original_connected_account(session, garage, monkeypatch):
    booking = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference="BKREFUND",
        customer_first_name="Alex",
        customer_last_name="Turner",
        customer_email="alex@example.com",
        customer_phone="+447123456789",
        vehicle_registration="CN11REF",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7),
        preferred_time=datetime.time(9, 30),
    )
    session.add(booking)
    session.flush()
    payment = BookingPayment(
        garage_id=garage.id,
        booking_request_id=booking.id,
        provider="stripe",
        provider_account_id="acct_original",
        provider_payment_id="pi_original",
        amount_minor=2000,
        currency="GBP",
        status="SUCCEEDED",
    )
    session.add(payment)
    session.commit()
    captured = {}

    class Provider:
        def refund_payment(self, payment_id, **kwargs):
            captured["payment_id"] = payment_id
            captured.update(kwargs)
            from app.payments.providers.base import RefundResult

            return RefundResult("re_1", "REFUNDED", 2000)

    monkeypatch.setattr(
        "app.payments.service.get_provider",
        lambda name, **kwargs: captured.update(kwargs) or Provider(),
    )
    refund_deposit(booking, reason="test")
    assert captured["connected_account_id"] == "acct_original"
    assert captured["payment_id"] == "pi_original"


def test_connect_webhook_route_handles_a_real_shaped_event_and_is_idempotent(
    app, client, session, garage, monkeypatch
):
    """End-to-end through the real HTTP route (not process_webhook called
    directly, and not the fake provider) - the exact path a genuine Stripe
    Connect delivery takes: POST /api/webhooks/payments/stripe/connect ->
    stripe_connect_webhook -> process_webhook -> StripePaymentProvider.
    verify_webhook. Uses a double shaped like the real stripe-python 15
    SDK's Event (to_dict() only; dict()/`.get()` raise, matching the
    production crash this was written to catch - see
    app/payments/providers/stripe_provider.py::verify_webhook and
    tests/test_payment_providers.py's unit-level equivalent), delivered
    twice to prove Stripe's own retry-on-slow-or-error behaviour can never
    double-process a payment.
    """
    from app.models.payments.garage_payment_settings import GaragePaymentSettings

    settings = GaragePaymentSettings(
        garage_id=garage.id, provider="stripe", stripe_account_id="acct_http_1"
    )
    booking = BookingRequest(
        garage_id=garage.id,
        status="AWAITING_PAYMENT",
        booking_reference="BKHTTPCON",
        customer_first_name="Alex",
        customer_last_name="Turner",
        customer_email="alex@example.com",
        customer_phone="+447123456789",
        vehicle_registration="CN11HTP",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7),
        preferred_time=datetime.time(9, 30),
        payment_hold_expires_at=datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(minutes=15),
    )
    session.add_all([settings, booking])
    session.flush()
    payment = BookingPayment(
        garage_id=garage.id,
        booking_request_id=booking.id,
        provider="stripe",
        provider_account_id="acct_http_1",
        provider_payment_id="pi_http_1",
        amount_minor=100,
        currency="GBP",
        status="REQUIRES_PAYMENT",
    )
    session.add(payment)
    session.commit()

    payload = {
        "id": "evt_http_dup_1",
        "type": "payment_intent.succeeded",
        "account": "acct_http_1",
        "data": {"object": {"id": "pi_http_1", "status": "succeeded"}},
    }

    class RealShapedEvent:
        """Models stripe-python 15's StripeObject: subscriptable, but
        neither ``dict()``/iteration nor ``.get()`` work - only
        ``.to_dict()`` does (see the production traceback this guards
        against)."""

        def __getitem__(self, key):
            return payload[key]

        def __iter__(self):
            raise TypeError("Event is not iterable or a mapping; call .to_dict() for a plain dict.")

        def get(self, _key):
            raise AssertionError("Stripe Event.get must not be called")

        def to_dict(self):
            return payload

    from types import SimpleNamespace

    fake_stripe = SimpleNamespace(
        Webhook=SimpleNamespace(construct_event=lambda *_args, **_kwargs: RealShapedEvent()),
        error=SimpleNamespace(SignatureVerificationError=Exception),
    )
    monkeypatch.setattr("app.payments.providers.stripe_provider._client", lambda: fake_stripe)
    app.config["STRIPE_CONNECT_WEBHOOK_SECRET"] = "whsec_test_connect"

    first = client.post(
        "/api/webhooks/payments/stripe/connect",
        data=b"{}",
        headers={"Stripe-Signature": "t=1,v1=fake"},
        content_type="application/json",
    )
    second = client.post(
        "/api/webhooks/payments/stripe/connect",
        data=b"{}",
        headers={"Stripe-Signature": "t=1,v1=fake"},
        content_type="application/json",
    )

    assert first.status_code == 200
    assert second.status_code == 200

    session.refresh(booking)
    session.refresh(payment)
    assert booking.status == "PENDING"
    assert payment.status == "SUCCEEDED"
    assert PaymentWebhookEvent.query.filter_by(id="evt_http_dup_1").count() == 1
