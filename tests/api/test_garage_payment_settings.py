"""Per-business payment provider selection - a garage can use a different
provider (or be disabled outright) from the platform default, without any
of that showing up in booking/deposit/refund logic itself.

POST /api/public/<slug>/booking-requests/deposit-intent is the observable
surface: everything about *which* provider handled a given garage's deposit
is decided by app/payments/settings.py::resolve_provider_name, read here
only through its effects.
"""

import datetime
import json

from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.models.payments.payment import BookingPayment


def _future_weekday(days_ahead=7):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday().isoformat()


def _deposit_type(session, garage):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="MOT",
        status="ACTIVE",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    session.add(t)
    session.commit()
    return t


def _payload(appt_type, **overrides):
    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex.turner@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "PB11 REQ",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
        "appointment_type_id": str(appt_type.id),
    }
    payload.update(overrides)
    return payload


def test_a_garage_with_no_settings_row_uses_the_platform_default_provider(client, session, garage):
    appt_type = _deposit_type(session, garage)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 201
    assert resp.get_json()["provider"] == "fake"  # PAYMENTS_PROVIDER default in TestConfig


def test_a_garage_can_be_configured_to_use_a_different_provider(client, session, garage):
    session.add(GaragePaymentSettings(garage_id=garage.id, provider="fake", enabled=True))
    session.commit()

    appt_type = _deposit_type(session, garage)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 201
    assert resp.get_json()["provider"] == "fake"


def test_a_garage_configured_for_an_unimplemented_provider_fails_cleanly(client, session, garage):
    session.add(GaragePaymentSettings(garage_id=garage.id, provider="paypal", enabled=True))
    session.commit()

    appt_type = _deposit_type(session, garage)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 503
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_deposits_disabled_at_the_garage_level_refuses_even_though_the_type_requires_one(
    client, session, garage
):
    session.add(GaragePaymentSettings(garage_id=garage.id, provider="fake", enabled=False))
    session.commit()

    appt_type = _deposit_type(session, garage)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 503


def test_garage_payment_settings_is_purely_additive_to_existing_garages(session, garage):
    """No row exists unless something explicitly creates one - a garage
    onboarded before this table existed is completely unaffected."""
    assert GaragePaymentSettings.query.filter_by(garage_id=garage.id).first() is None
    db.session.refresh(garage)
    assert garage.payment_settings is None


def test_webhook_uses_the_provider_that_actually_created_the_payment(client, session, garage):
    """A garage's provider setting could in principle change after a
    payment session was created - the payment row itself (not the garage's
    current settings) must decide which adapter handles its webhook."""
    session.add(GaragePaymentSettings(garage_id=garage.id, provider="fake", enabled=True))
    session.commit()

    appt_type = _deposit_type(session, garage)
    created = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    ).get_json()
    payment = BookingPayment.query.filter_by(booking_request_id=created["booking_request_id"]).one()
    assert payment.provider == "fake"

    resp = client.post(
        "/api/webhooks/payments/fake",
        data=json.dumps(
            {
                "id": "evt_settings_1",
                "type": "payment.succeeded",
                "provider_payment_id": payment.provider_payment_id,
                "status": "SUCCEEDED",
            }
        ),
        content_type="application/json",
        headers={"Fake-Signature": "valid"},
    )
    assert resp.status_code == 200
    session.refresh(payment)
    assert payment.status == "SUCCEEDED"
