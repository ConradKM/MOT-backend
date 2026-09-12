"""API tests for the Stripe(-shaped) payment webhook endpoint.

POST /api/webhooks/payments/stripe

Runs against the fake payment provider (PAYMENTS_PROVIDER=fake in
TestConfig, see app/payments/providers/fake.py) - "Fake-Signature: valid" is
the fake provider's stand-in for real Stripe signature verification, so
these tests exercise the same dispatch/idempotency code real Stripe events
go through, without any network call or real account.
"""

import datetime
import json

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.payment import BookingPayment
from app.models.payments.webhook_event import PaymentWebhookEvent


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


def _start_deposit(client, garage, appt_type, **overrides):
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
    return client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload
    ).get_json()


def _webhook(client, event, *, signature="valid"):
    return client.post(
        "/api/webhooks/payments/stripe",
        data=json.dumps(event),
        content_type="application/json",
        headers={"Fake-Signature": signature},
    )


def test_invalid_signature_is_rejected(client):
    resp = _webhook(
        client,
        {"id": "evt_1", "type": "payment_intent.succeeded", "provider_payment_id": "pi_x"},
        signature="nope",
    )
    assert resp.status_code == 400
    assert PaymentWebhookEvent.query.count() == 0


def test_payment_succeeded_flips_booking_request_to_pending(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    resp = _webhook(
        client,
        {
            "id": "evt_success_1",
            "type": "payment_intent.succeeded",
            "provider_payment_id": payment.provider_payment_id,
            "status": "succeeded",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert booking_request.payment_hold_expires_at is None
    assert payment.status == "SUCCEEDED"
    assert payment.paid_at is not None


def test_duplicate_webhook_event_is_a_no_op(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    event = {
        "id": "evt_dup_1",
        "type": "payment_intent.succeeded",
        "provider_payment_id": payment.provider_payment_id,
        "status": "succeeded",
    }
    first = _webhook(client, event)
    second = _webhook(client, event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert PaymentWebhookEvent.query.filter_by(id="evt_dup_1").count() == 1

    session.refresh(booking_request)
    assert booking_request.status == "PENDING"  # not double-processed into a broken state


def test_payment_failed_keeps_hold_open_for_retry(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    resp = _webhook(
        client,
        {
            "id": "evt_fail_1",
            "type": "payment_intent.payment_failed",
            "provider_payment_id": payment.provider_payment_id,
            "status": "requires_payment_method",
            "failure_message": "Your card was declined.",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert payment.status == "FAILED"
    assert payment.failure_reason == "Your card was declined."
    # Still held - the customer can retry with a fresh intent before the hold
    # itself expires.
    assert booking_request.status == "AWAITING_PAYMENT"


def test_webhook_for_unknown_payment_intent_is_ignored(client):
    resp = _webhook(
        client,
        {
            "id": "evt_unknown_1",
            "type": "payment_intent.succeeded",
            "provider_payment_id": "pi_does_not_exist",
            "status": "succeeded",
        },
    )
    assert resp.status_code == 200
    assert PaymentWebhookEvent.query.filter_by(id="evt_unknown_1").count() == 1
