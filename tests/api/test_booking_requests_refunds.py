"""API tests for deposit refunds on the staff-facing booking-request routes.

POST /api/booking-requests/<id>/reject   (auto-refunds a paid deposit)
POST /api/booking-requests/<id>/refund   (manual refund - cancellation infra)
"""

import datetime
import json

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
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


def _paid_booking_request(client, session, garage, appt_type):
    """Full round trip: start a deposit hold, then confirm it via a webhook,
    exactly like a real customer paying - so the payment row is genuinely
    SUCCEEDED (not just constructed in that state), matching what the reject
    route actually depends on."""
    created = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json={
            "customer_first_name": "Alex",
            "customer_last_name": "Turner",
            "customer_email": "alex.turner@example.com",
            "customer_phone": "07123 456789",
            "vehicle_registration": "PB11 REQ",
            "preferred_date": FUTURE_DATE,
            "preferred_time": "09:30:00",
            "appointment_type_id": str(appt_type.id),
        },
    ).get_json()

    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()

    client.post(
        "/api/webhooks/payments/stripe",
        data=json.dumps(
            {
                "id": f"evt_paid_{booking_request.id}",
                "type": "payment_intent.succeeded",
                "provider_payment_id": payment.provider_payment_id,
                "status": "succeeded",
            }
        ),
        content_type="application/json",
        headers={"Fake-Signature": "valid"},
    )
    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert payment.status == "SUCCEEDED"
    return booking_request, payment


def test_rejecting_a_paid_booking_refunds_the_deposit(authenticated_user, session):
    appt_type = _deposit_type(session, authenticated_user.garage)
    booking_request, payment = _paid_booking_request(
        authenticated_user.client, session, authenticated_user.garage, appt_type
    )

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/reject",
        json={"staff_notes": "Fully booked elsewhere."},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "REJECTED"

    session.refresh(payment)
    assert payment.status == "REFUNDED"
    assert payment.refunded_amount_minor == 2000
    assert payment.refunded_at is not None
    assert payment.provider_refund_id is not None


def test_rejecting_an_unpaid_booking_does_not_attempt_a_refund(authenticated_user, session):
    appt_type = GarageAppointmentType(
        garage_id=authenticated_user.garage.id, name="MOT", status="ACTIVE"
    )
    session.add(appt_type)
    session.commit()

    booking_request = BookingRequest(
        garage_id=authenticated_user.garage.id,
        status="PENDING",
        booking_reference="BKNODEPOSIT",
        customer_first_name="Pat",
        customer_last_name="Rivera",
        customer_email="pat.rivera@example.com",
        vehicle_registration="BK11REQ",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=5),
        preferred_time=datetime.time(9, 0),
    )
    session.add(booking_request)
    session.commit()

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/reject", json={}
    )
    assert resp.status_code == 200
    assert BookingPayment.query.count() == 0


def test_manual_refund_endpoint_refunds_a_paid_booking(authenticated_user, session):
    appt_type = _deposit_type(session, authenticated_user.garage)
    booking_request, payment = _paid_booking_request(
        authenticated_user.client, session, authenticated_user.garage, appt_type
    )

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/refund",
        json={"reason": "customer cancelled"},
    )
    assert resp.status_code == 200

    session.refresh(payment)
    assert payment.status == "REFUNDED"


def test_manual_refund_endpoint_rejects_a_booking_with_no_successful_payment(
    authenticated_user, session
):
    booking_request = BookingRequest(
        garage_id=authenticated_user.garage.id,
        status="PENDING",
        booking_reference="BKNOPAY0001",
        customer_first_name="Pat",
        customer_last_name="Rivera",
        customer_email="pat.rivera@example.com",
        vehicle_registration="BK22REQ",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=5),
        preferred_time=datetime.time(9, 0),
    )
    session.add(booking_request)
    session.commit()

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking_request.id}/refund", json={}
    )
    assert resp.status_code == 409


def test_booking_request_detail_shows_payment_status(authenticated_user, session):
    appt_type = _deposit_type(session, authenticated_user.garage)
    booking_request, _payment = _paid_booking_request(
        authenticated_user.client, session, authenticated_user.garage, appt_type
    )

    resp = authenticated_user.client.get(f"/api/booking-requests/{booking_request.id}")
    body = resp.get_json()
    assert body["payment"]["status"] == "SUCCEEDED"
    assert body["payment"]["amount"] == "20.00"
