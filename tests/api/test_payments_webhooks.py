"""API tests for the provider-neutral payment webhook endpoint.

POST /api/webhooks/payments/<provider>

Runs against the fake payment provider (PAYMENTS_PROVIDER=fake in
TestConfig, see app/payments/providers/fake.py) via
``/api/webhooks/payments/fake`` - "Fake-Signature: valid" is the fake
provider's stand-in for real signature verification, and its own event
vocabulary (payment.succeeded/failed/cancelled, refund.updated) is mapped to
CoMaz's normalised ``kind``s exactly like a real adapter would map its own
provider's names, so these tests exercise the same dispatch/idempotency/
normalisation code any real provider's events go through, without any
network call or real account.
"""

import datetime
import json

from app.extensions import db
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


def _webhook(client, event, *, provider="fake", signature="valid"):
    return client.post(
        f"/api/webhooks/payments/{provider}",
        data=json.dumps(event),
        content_type="application/json",
        headers={"Fake-Signature": signature},
    )


def test_invalid_signature_is_rejected(client):
    resp = _webhook(
        client,
        {"id": "evt_1", "type": "payment.succeeded", "provider_payment_id": "pi_x"},
        signature="nope",
    )
    assert resp.status_code == 400
    assert PaymentWebhookEvent.query.count() == 0


def test_an_unconfigured_provider_is_rejected_cleanly(client):
    # "stripe" has no real credentials in the test suite (PAYMENTS_PROVIDER
    # is "fake" - see TestConfig) - a delivery for it must degrade to a
    # clear 503, never a 500, exactly like the public deposit-intent
    # endpoint does for an unconfigured provider.
    resp = _webhook(client, {"id": "evt_x", "type": "payment.succeeded"}, provider="stripe")
    assert resp.status_code == 503
    assert PaymentWebhookEvent.query.count() == 0


def test_an_unknown_provider_name_is_rejected_cleanly(client):
    # Same 503 as any other not-configured provider - "unknown" and "not
    # implemented" both collapse into the same clear, crash-free signal
    # rather than a 500.
    resp = _webhook(client, {"id": "evt_x", "type": "payment.succeeded"}, provider="venmo")
    assert resp.status_code == 503


def test_payment_succeeded_flips_booking_request_to_pending(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()
    assert payment.provider == "fake"

    resp = _webhook(
        client,
        {
            "id": "evt_success_1",
            "type": "payment.succeeded",
            "provider_payment_id": payment.provider_payment_id,
            "status": "SUCCEEDED",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert booking_request.payment_hold_expires_at is None
    assert payment.status == "SUCCEEDED"
    assert payment.paid_at is not None

    # The audit ledger stores the normalised kind, not the provider's own
    # event-type spelling - see app/payments/service.py::process_webhook.
    stored = db.session.get(PaymentWebhookEvent, "evt_success_1")
    assert stored.event_type == "payment.succeeded"


def test_belated_success_reinstates_an_expired_booking_if_the_slot_is_still_free(
    client, session, garage
):
    """A webhook that only arrives (or only succeeds) after the hold had
    already expired must not just leave a real charge attached to a dead
    booking - if nothing else took the slot meanwhile, the booking is
    reinstated exactly as a normal success would. See
    app/payments/service.py::_reinstate_or_flag_expired_booking."""
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    # Simulate the hold having already expired before the webhook arrived.
    booking_request.status = "EXPIRED"
    booking_request.payment_hold_expires_at = None
    session.commit()

    resp = _webhook(
        client,
        {
            "id": "evt_belated_1",
            "type": "payment.succeeded",
            "provider_payment_id": payment.provider_payment_id,
            "status": "SUCCEEDED",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert payment.status == "SUCCEEDED"


def test_belated_success_leaves_a_taken_slot_expired_but_still_marks_payment_succeeded(
    client, session, garage, make_appointment
):
    """If someone else has genuinely taken the slot by the time a belated
    success arrives, the booking must not be silently resurrected on top of
    them - but the payment record must still show the real charge, so staff
    have something to act on (refund or manual rebook) instead of a payment
    that just vanishes."""
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    booking_request.status = "EXPIRED"
    booking_request.payment_hold_expires_at = None
    session.commit()

    # Someone else's real, confirmed appointment now occupies that exact slot.
    start = datetime.datetime.combine(
        booking_request.preferred_date, booking_request.preferred_time, tzinfo=datetime.UTC
    )
    make_appointment(start, minutes=appt_type.default_duration_minutes or 60)

    resp = _webhook(
        client,
        {
            "id": "evt_belated_2",
            "type": "payment.succeeded",
            "provider_payment_id": payment.provider_payment_id,
            "status": "SUCCEEDED",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "EXPIRED"  # not silently resurrected onto a taken slot
    assert payment.status == "SUCCEEDED"  # but the real charge is still on record


def test_duplicate_webhook_event_is_a_no_op(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    event = {
        "id": "evt_dup_1",
        "type": "payment.succeeded",
        "provider_payment_id": payment.provider_payment_id,
        "status": "SUCCEEDED",
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
            "type": "payment.failed",
            "provider_payment_id": payment.provider_payment_id,
            "status": "REQUIRES_PAYMENT",
            "failure_message": "Your card was declined.",
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert payment.status == "FAILED"
    assert payment.failure_reason == "Your card was declined."
    # Still held - the customer can retry with a fresh session before the
    # hold itself expires.
    assert booking_request.status == "AWAITING_PAYMENT"


def test_webhook_for_unknown_payment_is_ignored(client):
    resp = _webhook(
        client,
        {
            "id": "evt_unknown_1",
            "type": "payment.succeeded",
            "provider_payment_id": "pay_does_not_exist",
            "status": "SUCCEEDED",
        },
    )
    assert resp.status_code == 200
    assert PaymentWebhookEvent.query.filter_by(id="evt_unknown_1").count() == 1


def test_unhandled_event_kind_is_recorded_but_causes_no_state_change(client, session, garage):
    appt_type = _deposit_type(session, garage)
    created = _start_deposit(client, garage, appt_type)
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    resp = _webhook(
        client,
        {
            "id": "evt_unhandled_1",
            "type": "payment.something_else_entirely",
            "provider_payment_id": payment.provider_payment_id,
        },
    )
    assert resp.status_code == 200

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "AWAITING_PAYMENT"
    assert payment.status == "REQUIRES_PAYMENT"
    assert db.session.get(PaymentWebhookEvent, "evt_unhandled_1").event_type == "unhandled"
