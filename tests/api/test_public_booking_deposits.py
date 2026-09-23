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


def test_deposit_attempt_cannot_be_reused_for_a_changed_service_or_slot(client, session, garage):
    first_type = _deposit_type(session, garage, base_price="100.00")
    second_type = _deposit_type(session, garage, base_price="200.00")
    attempt_id = str(uuid.uuid4())
    payload = _payload(first_type, payment_attempt_id=attempt_id)
    assert (
        client.post(
            f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload
        ).status_code
        == 201
    )

    changed_service = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(second_type, payment_attempt_id=attempt_id),
    )
    assert changed_service.status_code == 409
    assert changed_service.get_json()["errors"] == {"reason": "payment_attempt_mismatch"}

    changed_slot = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(first_type, payment_attempt_id=attempt_id, preferred_time="10:30:00"),
    )
    assert changed_slot.status_code == 409
    assert changed_slot.get_json()["errors"] == {"reason": "payment_attempt_mismatch"}
    assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 1
    assert BookingPayment.query.filter_by(garage_id=garage.id).count() == 1


def test_resumed_deposit_attempt_uses_its_original_price_snapshot(client, session, garage):
    appt_type = _deposit_type(session, garage, base_price="100.00")
    payload = _payload(appt_type, payment_attempt_id=str(uuid.uuid4()))
    first = client.post(f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload)
    assert first.status_code == 201

    # Staff changing a catalogue price must not rewrite an in-flight
    # customer's total when their browser retries the same checkout.
    appt_type.base_price = "250.00"
    session.commit()
    resumed = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload
    )
    assert resumed.status_code == 201
    assert resumed.get_json()["service_total"] == "100.00"
    assert resumed.get_json()["deposit_amount"] == "20.00"
    assert resumed.get_json()["remaining_balance"] == "80.00"
    poll = client.get(
        f"/api/public/{garage.slug}/booking-requests/"
        f"{resumed.get_json()['booking_reference']}/payment-status"
    )
    assert poll.status_code == 200
    assert poll.get_json()["service_total"] == "100.00"
    assert poll.get_json()["remaining_balance"] == "80.00"


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

    # The reason is also machine-readable in the response itself, not just
    # the server log - see app/public_booking/routes.py::_lock_and_validate_slot.
    assert second.get_json()["errors"] == {"reason": "full"}


def test_adjacent_slots_touching_boundary_both_succeed(client, session, garage):
    """Regression guard for a production report: a customer saw an
    advertised slot get rejected as unavailable at Deposit, and suspected an
    off-by-one at the exact moment one booking ends and the next begins
    (e.g. 11:00-12:30 then 12:30-14:00 back to back). ``_slot_usage`` uses a
    strict half-open interval (``start < other_end and end > other_start``),
    so a slot that starts exactly when the previous one ends must never be
    treated as occupied by it - confirmed here at the actual deposit-intent
    validation layer, not just in the advertised calendar."""
    appt_type = _deposit_type(session, garage, deposit_value="20.00")
    appt_type.default_duration_minutes = 90
    session.commit()

    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, preferred_time="11:00:00"),
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type,
            preferred_time="12:30:00",
            customer_email="second@example.com",
            vehicle_registration="ZZ99 ZZZ",
        ),
    )
    assert second.status_code == 201


def test_slot_starting_one_minute_before_the_previous_ends_genuinely_conflicts(
    client, session, garage
):
    """The mirror image of the boundary test above: a candidate that starts
    even one minute before the prior booking's end must be rejected as a
    genuine overlap, proving the boundary is exact and not merely lenient in
    both directions."""
    appt_type = _deposit_type(session, garage, deposit_value="20.00")
    appt_type.default_duration_minutes = 90
    session.commit()

    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, preferred_time="11:00:00"),
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type,
            preferred_time="12:29:00",
            customer_email="second@example.com",
            vehicle_registration="ZZ99 ZZZ",
        ),
    )
    assert second.status_code == 409


def test_vehicle_reference_conflict_is_not_reported_as_slot_unavailable(client, session, garage):
    """Regression guard for a real production incident: a customer's booking
    was rejected with a 409 that MOT-frontend's DepositStep unconditionally
    displayed as "This time is no longer available", even though the actual
    cause (app/booking_requests/service.py::resolve_customer_and_vehicle)
    was a different customer already owning a vehicle with the same
    registration at this garage - unrelated to the slot itself, and not
    logged by AVAILABILITY_REJECTED since it never goes through
    app/public_booking/routes.py::_lock_and_validate_slot. Two different
    customers at two genuinely non-conflicting times, sharing a
    registration, must 409 with a distinct machine-readable reason - not
    "full" - so the frontend can tell the two apart."""
    appt_type = _deposit_type(session, garage)

    first = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, preferred_time="09:30:00", vehicle_registration="SH4 RED"),
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(
            appt_type,
            preferred_time="11:00:00",
            customer_email="different-customer@example.com",
            vehicle_registration="SH4 RED",
        ),
    )
    assert second.status_code == 409
    body = second.get_json()
    assert body["errors"] == {"reason": "vehicle_reference_conflict"}
    assert "request_id" in body


def test_concurrent_identical_deposit_attempts_both_resolve_without_a_false_conflict(app, garage):
    """The dangerous sequence the P0 mandate asked us to prove or disprove:
    request A creates the hold; request B, arriving milliseconds later with
    the *same* logical attempt (same payment_attempt_id - one browser tab's
    single click, however many HTTP requests it actually produces), must
    resume A's hold rather than being told the slot it just reserved is
    unavailable. app/public_booking/routes.py::DepositIntentCreate.post takes
    a per-garage row lock (``with_for_update``) before checking for an
    existing attempt, which serialises the two requests rather than letting
    them race - this drives two real concurrent HTTP requests through the
    actual Flask app (a thread each, real threads, not sequential calls) to
    prove that serialisation holds under genuine concurrency, not just when
    called one after another in the same test."""
    import uuid as uuid_mod
    from concurrent.futures import ThreadPoolExecutor

    from app.extensions import db

    with app.app_context():
        appt_type = _deposit_type(db.session, garage)
        appt_type_id = str(appt_type.id)
    attempt_id = str(uuid_mod.uuid4())

    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex.turner@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "PB11 REQ",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
        "appointment_type_id": appt_type_id,
        "payment_attempt_id": attempt_id,
    }

    def _post():
        with app.test_client() as c:
            return c.post(
                f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _post(), range(2)))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 201], [r.get_json() for r in results]
    references = {r.get_json()["booking_reference"] for r in results}
    assert len(references) == 1, "both requests must resolve to the SAME booking, not two"

    with app.app_context():
        assert BookingRequest.query.filter_by(garage_id=garage.id).count() == 1


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


def test_expiry_never_releases_a_hold_with_a_locally_successful_payment(
    app, session, garage, client
):
    """Regression for the webhook/expiry race: the old deadline is not a
    licence to expire a payment row which has already reached SUCCEEDED."""
    appt_type = _deposit_type(session, garage)
    create = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=_payload(appt_type)
    ).get_json()
    booking_request = BookingRequest.query.filter_by(
        booking_reference=create["booking_reference"]
    ).one()
    payment = BookingPayment.query.filter_by(booking_request_id=booking_request.id).one()
    payment.status = "SUCCEEDED"
    payment.paid_at = datetime.datetime.now(datetime.UTC)
    booking_request.payment_hold_expires_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(minutes=1)
    session.commit()

    assert expire_stale_payment_holds(garage_id=garage.id) == 0
    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "PENDING"
    assert booking_request.payment_hold_expires_at is None
    assert payment.status == "SUCCEEDED"


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
