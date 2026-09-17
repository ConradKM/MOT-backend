"""Stripe Connect seams: tenant ownership, readiness, and Direct Charges.

These tests intentionally use small Stripe/provider fakes.  They exercise
CoMaz's boundary and never need a Stripe account or network access.
"""

import datetime

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.models.payments.payment import BookingPayment
from app.payments.connect import create_connected_account, refresh_connect_status
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


def test_connect_account_creation_and_status_refresh(session, garage, monkeypatch):
    api = _AccountApi()
    stripe = type("Stripe", (), {"Account": api})()
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)

    assert create_connected_account(garage) == "acct_connect_a"
    assert create_connected_account(garage) == "acct_connect_a"  # idempotent
    assert len(api.created) == 1
    assert api.created[0]["email"] == garage.email

    settings = refresh_connect_status(garage)
    assert settings.stripe_onboarding_complete is True
    assert settings.stripe_charges_enabled is True
    assert settings.stripe_payouts_enabled is True


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
    monkeypatch.setattr("app.payments.connect._client", lambda: type("Stripe", (), {"Account": api})())

    create_connected_account(garage)
    settings = refresh_connect_status(garage)

    assert settings.stripe_onboarding_complete is True
    assert settings.stripe_charges_enabled is True
    assert settings.stripe_payouts_enabled is True


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
    session.add(payment)
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
    assert settings.stripe_charges_enabled is True
    assert payment.status == "SUCCEEDED"  # the wrong-account event was ignored


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
