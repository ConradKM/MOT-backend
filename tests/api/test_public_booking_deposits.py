"""API tests for the deposit payment flow on top of public booking.

POST /api/public/<slug>/booking-requests/deposit-intent
GET  /api/public/<slug>/booking-requests/<reference>/payment-status
"""

import datetime
import uuid

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.payment import BookingPayment
from app.payments.service import expire_stale_payment_holds


def _future_weekday(days_ahead=7):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday().isoformat()


def _deposit_type(
    session, garage, *, deposit_type="FIXED", deposit_value="20.00", base_price="100.00"
):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="MOT",
        status="ACTIVE",
        base_price=base_price,
        deposit_required=True,
        deposit_type=deposit_type,
        deposit_value=deposit_value,
    )
    session.add(t)
    session.commit()
    return t


def _no_deposit_type(session, garage):
    t = GarageAppointmentType(garage_id=garage.id, name="Diagnostic", status="ACTIVE")
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


def test_deposit_intent_creates_awaiting_payment_hold(client, session, garage):
    appt_type = _deposit_type(session, garage)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "AWAITING_PAYMENT"
    assert body["payment_status"] == "REQUIRES_PAYMENT"
    assert body["currency"] == "GBP"
    assert body["deposit_amount"] == "20.00"
    assert body["service_total"] == "100.00"
    assert body["remaining_balance"] == "80.00"
    assert body["provider"] == "fake"
    assert body["checkout_mode"] == "EMBEDDED"
    assert body["provider_data"]["client_secret"] is not None
    assert body["booking_reference"]

    booking_request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking_request.status == "AWAITING_PAYMENT"
    assert booking_request.payment_hold_expires_at is not None

    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()
    assert payment.amount_minor == 2000
    assert payment.status == "REQUIRES_PAYMENT"
    assert payment.provider_payment_id is not None


def test_deposit_intent_percentage_calculation(client, session, garage):
    appt_type = _deposit_type(
        session, garage, deposit_type="PERCENTAGE", deposit_value="25", base_price="100.00"
    )

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 201
    assert resp.get_json()["deposit_amount"] == "25.00"


def test_deposit_intent_ignores_client_supplied_amount(client, session, garage):
    """The deposit is always recalculated server-side - nothing in the
    public payload can influence amount_minor even if a field named like one
    were sent."""
    appt_type = _deposit_type(session, garage)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, amount_minor=1, deposit_amount="0.01"),
    )
    assert resp.status_code == 201
    assert resp.get_json()["deposit_amount"] == "20.00"


def test_plain_submit_rejects_a_deposit_required_type(client, session, garage):
    appt_type = _deposit_type(session, garage)

    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload(appt_type))
    assert resp.status_code == 422
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 0


def test_deposit_intent_rejects_a_non_deposit_type(client, session, garage):
    appt_type = _no_deposit_type(session, garage)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert resp.status_code == 422


def test_deposit_hold_reserves_capacity_for_another_customer(client, session, garage):
    appt_type = _deposit_type(session, garage)

    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    )
    assert first.status_code == 201

    # One employee -> default per-slot capacity of 1; the same slot should
    # now be unavailable to a second customer even though nobody has PENDING
    # status yet - the AWAITING_PAYMENT hold itself reserves it.
    second = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(
            appt_type,
            appointment_type_id=None,
            customer_email="second@example.com",
            vehicle_registration="ZZ99 ZZZ",
        ),
    )
    assert second.status_code == 409


def test_retried_deposit_attempt_resumes_its_own_hold(client, session, garage):
    """A duplicate browser request must not reject its own capacity hold or
    create another PaymentIntent/BookingPayment."""
    appt_type = _deposit_type(session, garage)
    payload = _payload(appt_type, payment_attempt_id=str(uuid.uuid4()))

    first = client.post(f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload)
    second = client.post(f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload)

    assert first.status_code == second.status_code == 201
    assert second.get_json()["booking_reference"] == first.get_json()["booking_reference"]
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 1
    assert BookingPayment.query.filter_by(garage_id=garage.id).count() == 1


def test_different_deposit_attempt_is_blocked_by_active_hold(client, session, garage):
    appt_type = _deposit_type(session, garage)
    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, payment_attempt_id=str(uuid.uuid4())),
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type,
            customer_email="second@example.com",
            vehicle_registration="ZZ99 ZZZ",
            payment_attempt_id=str(uuid.uuid4()),
        ),
    )
    assert second.status_code == 409


def test_full_rejection_logs_diagnostic_capacity_context(client, session, garage, caplog):
    """A genuine capacity rejection must leave enough evidence to root-cause
    it after the fact - see app/public_booking/routes.py::
    _lock_and_validate_slot. Byte-counting an access log (what production
    forensics was reduced to before this existed) can't tell two different
    409 reasons apart; this makes the exact reason and capacity snapshot
    explicit in the application log at the moment of rejection."""
    appt_type = _deposit_type(session, garage)
    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, payment_attempt_id=str(uuid.uuid4())),
    )
    assert first.status_code == 201

    with caplog.at_level("WARNING"):
        second = client.post(
            f"/api/public/{garage.slug}/booking-requests/deposit-intent",
            json=_payload(
                appt_type,
                customer_email="second@example.com",
                vehicle_registration="ZZ99 ZZZ",
                payment_attempt_id=str(uuid.uuid4()),
            ),
        )
    assert second.status_code == 409

    [record] = [r for r in caplog.records if "AVAILABILITY_REJECTED" in r.message]
    assert "reason=full" in record.message
    assert "used=1 capacity=1" in record.message
    assert str(garage.id) in record.message


def test_status_poll_reflects_hold_until_paid(client, session, garage):
    appt_type = _deposit_type(session, garage)
    create = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    ).get_json()

    poll = client.get(
        f"/api/public/{garage.slug}/booking-requests/{create['booking_reference']}/payment-status"
    )
    assert poll.status_code == 200
    body = poll.get_json()
    assert body["status"] == "AWAITING_PAYMENT"
    assert body["payment_status"] == "REQUIRES_PAYMENT"
    # A fresh client_secret is never handed out twice.
    assert "client_secret" not in body


def test_status_poll_unknown_reference_is_404(client, garage):
    resp = client.get(f"/api/public/{garage.slug}/booking-requests/BKNOPE1234/payment-status")
    assert resp.status_code == 404


def test_expired_hold_releases_capacity_and_cancels_the_intent(app, session, garage, client):
    appt_type = _deposit_type(session, garage)
    create = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    ).get_json()

    booking_request = BookingRequest.query.filter_by(
        booking_reference=create["booking_reference"]
    ).one()

    far_future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    changed = expire_stale_payment_holds(garage_id=garage.id, now=far_future)
    assert changed == 1

    session.refresh(booking_request)
    assert booking_request.status == "EXPIRED"
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()
    assert payment.status == "CANCELLED"

    # The slot is free again for a new customer.
    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type, customer_email="second@example.com", vehicle_registration="ZZ99 ZZZ"
        ),
    )
    assert second.status_code == 201


def test_expiry_check_reconciles_a_payment_that_actually_succeeded(app, session, garage, client):
    """A missed/delayed webhook must never leave a customer who genuinely
    paid with their booking EXPIRED and their payment CANCELLED - see
    app/payments/service.py::_reconcile_if_already_succeeded. Simulates the
    exact split found live in production: the provider's own record of the
    payment is SUCCEEDED, but nothing has told CoMaz that yet, and the hold
    then expires before anything does.
    """
    from app.payments.providers.fake import FakePaymentProvider

    appt_type = _deposit_type(session, garage)
    create = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    ).get_json()

    booking_request = BookingRequest.query.filter_by(
        booking_reference=create["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    # The provider genuinely completed the charge - as if Stripe had already
    # confirmed it, independent of whether CoMaz's webhook has processed it.
    FakePaymentProvider._sessions[payment.provider_payment_id]["status"] = "SUCCEEDED"

    far_future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    changed = expire_stale_payment_holds(garage_id=garage.id, now=far_future)
    assert changed == 0  # reconciled as a success, not counted as an expiry

    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert booking_request.payment_hold_expires_at is None
    assert payment.status == "SUCCEEDED"
    assert payment.paid_at is not None

    # The slot is genuinely taken now - a second customer cannot claim it.
    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type, customer_email="second@example.com", vehicle_registration="ZZ99 ZZZ"
        ),
    )
    assert second.status_code == 409
