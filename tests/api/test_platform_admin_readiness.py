"""The go-live checklist (/tenants/<id>/readiness): never fabricated from ID
presence alone - a Stripe account with charges disabled must report
not-ready, exactly as it should before a business's first real deposit."""

from datetime import time

from app.models.garage_schedule import GarageOpeningHours
from app.models.payments.garage_payment_settings import GaragePaymentSettings


def test_fresh_business_reports_not_ready_everywhere(platform_client, garage, user):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness")

    assert response.status_code == 200
    body = response.json
    assert body["garage_id"] == str(garage.id)

    business = {c["key"]: c["ok"] for c in body["business"]}
    assert business["business_created"] is True
    assert business["owner_exists"] is True
    assert business["services_configured"] is False
    assert body["business_ready"] is False

    payments = {c["key"]: c["ok"] for c in body["payments"]}
    assert payments["stripe_connected"] is False
    assert body["payments_ready"] is False
    assert body["ready_to_take_deposits"] is False


def test_stripe_account_with_charges_disabled_is_not_ready(platform_client, garage, user, session):
    """A connected-but-not-chargeable account must not read as ready - the
    exact fabrication this endpoint exists to avoid."""
    session.add(
        GaragePaymentSettings(
            garage_id=garage.id,
            stripe_account_id="acct_test123",
            stripe_onboarding_complete=False,
            stripe_charges_enabled=False,
            stripe_payouts_enabled=False,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    payments = {c["key"]: c["ok"] for c in body["payments"]}
    assert payments["stripe_connected"] is True
    assert payments["stripe_charges_enabled"] is False
    assert body["payments_ready"] is False
    assert body["ready_to_take_deposits"] is False


def test_communications_is_optional_for_public_booking_readiness(
    platform_client, garage, user, appointment_type, session
):
    """A business can take public bookings with zero comms configured -
    ready_for_public_booking must not depend on the communications section."""
    session.add(
        GarageOpeningHours(
            garage_id=garage.id,
            weekday=0,
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            is_closed=False,
        )
    )
    session.commit()

    body = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/readiness").json

    assert body["communications_ready"] is False
    assert body["business_ready"] is True
    assert body["ready_for_public_booking"] is True
