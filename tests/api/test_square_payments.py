"""Square end to end through the HTTP API: owner OAuth connection, provider
selection, the two-phase public deposit (intent -> token submission ->
webhook), refunds, and tenant isolation. Square's API is the in-process fake
in tests/square_helpers.py; everything above it is real code.
"""

import datetime
import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.employee import Employee
from app.models.payments.audit_log import PaymentAuditLog
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.models.payments.payment import BookingPayment
from app.models.payments.webhook_event import PaymentWebhookEvent
from app.payments import square_oauth
from app.payments.service import expire_stale_payment_holds, refund_deposit
from tests import square_helpers as sq
from tests.conftest import DEFAULT_PASSWORD, AuthenticatedClient


@pytest.fixture()
def fake_square(app):
    fake = sq.install(app)
    yield fake
    sq.uninstall(app)


@pytest.fixture()
def staff_client(client, session, garage, staff_role):
    staff = Employee(
        garage_id=garage.id,
        email="staff@garage-a.example",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    return AuthenticatedClient(client, create_access_token(identity=str(staff.id)))


def _future_weekday():
    day = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    return day.isoformat()


def _deposit_type(session, garage):
    item = GarageAppointmentType(
        garage_id=garage.id,
        name="Square MOT",
        status="ACTIVE",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    session.add(item)
    session.commit()
    return item


def _start_deposit(client, garage, item, **overrides):
    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "SQ11 ARE",
        "preferred_date": _future_weekday(),
        "preferred_time": "09:30:00",
        "appointment_type_id": str(item.id),
        **overrides,
    }
    return client.post(f"/api/public/{garage.slug}/booking-requests/deposit-intent", json=payload)


def _submit(client, garage, reference, source_id="cnon:card-ok"):
    return client.post(
        f"/api/public/{garage.slug}/booking-requests/{reference}/payments/token",
        json={"source_id": source_id, "verification_token": "verf:sca-ok"},
    )


def _square_webhook(client, body):
    return client.post(
        "/api/webhooks/payments/square",
        data=body,
        content_type="application/json",
        headers={"x-square-hmacsha256-signature": sq.sign(body)},
    )


def _payment_event(fake, payment_id, status, *, merchant_id=None, event_id=None):
    return sq.webhook_body(
        "payment.updated",
        {"id": payment_id, "status": status},
        merchant_id=merchant_id or fake.merchant_id,
        event_id=event_id,
    )


@pytest.fixture()
def square_garage(session, garage, fake_square):
    sq.connect_garage(session, garage, fake_square)
    return garage


@pytest.fixture()
def submitted(client, session, square_garage, fake_square):
    """A Square deposit taken as far as card submission (PENDING)."""
    item = _deposit_type(session, square_garage)
    created = _start_deposit(client, square_garage, item).get_json()
    submit = _submit(client, square_garage, created["booking_reference"])
    assert submit.status_code == 202, submit.get_json()
    booking_request = BookingRequest.query.filter_by(
        booking_reference=created["booking_reference"]
    ).one()
    payment = booking_request.active_payment
    return booking_request, payment


# --- OAuth connect ----------------------------------------------------------


def test_connect_returns_square_authorise_url_with_signed_state(
    authenticated_client, fake_square, garage
):
    response = authenticated_client.post("/api/payments/square/connect")
    assert response.status_code == 200
    url = urlparse(response.get_json()["url"])
    assert url.netloc == "connect.squareupsandbox.com"
    assert url.path == "/oauth2/authorize"
    query = parse_qs(url.query)
    assert query["client_id"] == [sq.APPLICATION_ID]
    assert query["scope"] == ["PAYMENTS_WRITE PAYMENTS_READ MERCHANT_PROFILE_READ"]
    assert query["session"] == ["false"]
    assert square_oauth.read_state(query["state"][0])["g"] == str(garage.id)
    assert sq.APPLICATION_SECRET not in response.get_data(as_text=True)


def test_connect_is_blocked_without_an_encryption_key(app, authenticated_client, fake_square):
    app.config["PAYMENTS_SECRET_KEY"] = ""
    try:
        response = authenticated_client.post("/api/payments/square/connect")
    finally:
        from app.config import TestConfig

        app.config["PAYMENTS_SECRET_KEY"] = TestConfig.PAYMENTS_SECRET_KEY
    assert response.status_code == 503
    assert response.get_json()["errors"]["reason"] == "secrets_not_configured"


def test_connect_is_unavailable_when_square_is_not_configured(authenticated_client):
    assert authenticated_client.post("/api/payments/square/connect").status_code == 503


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/payments/square/connect", None),
        ("get", "/api/payments/square/status", None),
        ("put", "/api/payments/square/location", {"location_id": "LOC_GB_1"}),
        ("post", "/api/payments/square/disconnect", None),
        ("get", "/api/payments/provider", None),
        ("put", "/api/payments/provider", {"provider": "square"}),
    ],
)
def test_every_square_settings_endpoint_is_owner_only(
    client, staff_client, customer_client, fake_square, method, path, body
):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(staff_client, method)(path, **kwargs).status_code == 403
    assert getattr(customer_client, method)(path, **kwargs).status_code == 403


def test_a_platform_admin_token_cannot_reach_business_settings(platform_client, fake_square):
    assert platform_client.get("/api/payments/square/status").status_code in (401, 403, 422)


def _state_for(garage, user):
    return square_oauth.make_state(garage.id, user.id)


def test_callback_happy_path_stores_encrypted_tokens_and_redirects(
    client, session, garage, user, fake_square
):
    response = client.get(
        "/api/payments/square/oauth/callback",
        query_string={"code": "good-code", "state": _state_for(garage, user)},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/{garage.id}/settings/payments?square=return")

    settings = GaragePaymentSettings.query.filter_by(garage_id=garage.id).one()
    assert settings.square_status == "CONNECTED"
    assert settings.square_merchant_id == fake_square.merchant_id
    assert settings.square_location_id == "LOC_GB_1"  # the only eligible one
    assert "EAAA" not in settings.square_access_token_enc
    assert "EQAA" not in settings.square_refresh_token_enc
    assert settings.square_token_expires_at > datetime.datetime.now(datetime.UTC)
    # Connecting never silently switches the active provider.
    assert settings.provider == "fake"
    audit = PaymentAuditLog.query.filter_by(action="payment.square.connected").one()
    assert "EAAA" not in json.dumps(audit.details)


def test_callback_with_several_locations_leaves_the_choice_to_the_owner(
    client, garage, user, fake_square
):
    fake_square.locations.append(
        {
            "id": "LOC_GB_2",
            "name": "Second site",
            "status": "ACTIVE",
            "currency": "GBP",
            "capabilities": ["CREDIT_CARD_PROCESSING"],
        }
    )
    client.get(
        "/api/payments/square/oauth/callback",
        query_string={"code": "good-code", "state": _state_for(garage, user)},
    )
    assert (
        GaragePaymentSettings.query.filter_by(garage_id=garage.id).one().square_location_id is None
    )


@pytest.mark.parametrize(
    "error_param,expected",
    [({"error": "access_denied"}, "denied"), ({"code": "bad-code"}, "token_exchange")],
)
def test_denied_or_failed_authorisation_redirects_with_a_code_and_stores_nothing(
    client, garage, user, fake_square, error_param, expected
):
    response = client.get(
        "/api/payments/square/oauth/callback",
        query_string={"state": _state_for(garage, user), **error_param},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"square=return&square_error={expected}")
    settings = GaragePaymentSettings.query.filter_by(garage_id=garage.id).first()
    assert settings is None or settings.square_access_token_enc is None


def test_tampered_state_is_rejected(client, garage, user, fake_square):
    state = _state_for(garage, user)
    response = client.get(
        "/api/payments/square/oauth/callback",
        query_string={"code": "good-code", "state": state[:-3] + "abc"},
    )
    assert response.status_code == 400
    assert GaragePaymentSettings.query.count() == 0


def test_expired_state_is_rejected(client, garage, user, fake_square, monkeypatch):
    state = _state_for(garage, user)
    monkeypatch.setattr(square_oauth, "STATE_MAX_AGE_SECONDS", -1)
    response = client.get(
        "/api/payments/square/oauth/callback",
        query_string={"code": "good-code", "state": state},
    )
    assert response.status_code == 400
    assert GaragePaymentSettings.query.count() == 0


def test_state_for_another_garage_cannot_connect_it(
    client, garage, second_garage, user, fake_square
):
    # Garage A's owner, but a state claiming garage B.
    state = square_oauth.make_state(second_garage.id, user.id)
    response = client.get(
        "/api/payments/square/oauth/callback", query_string={"code": "good-code", "state": state}
    )
    assert response.status_code == 400
    assert GaragePaymentSettings.query.count() == 0


def test_one_square_merchant_cannot_connect_two_businesses(
    client, session, garage, second_garage, second_user, fake_square
):
    sq.connect_garage(session, garage, fake_square)
    response = client.get(
        "/api/payments/square/oauth/callback",
        query_string={
            "code": "good-code",
            "state": square_oauth.make_state(second_garage.id, second_user.id),
        },
    )
    assert response.headers["Location"].endswith("square_error=merchant_in_use")
    assert GaragePaymentSettings.query.filter_by(garage_id=second_garage.id).first() is None


# --- status / location / disconnect -----------------------------------------


def test_status_reports_connection_without_any_secret(
    authenticated_client, square_garage, fake_square
):
    response = authenticated_client.get("/api/payments/square/status")
    body = response.get_json()
    assert body["status"] == "CONNECTED"
    assert body["ready"] is True
    assert body["merchant_name"] == "Test Motors Ltd"
    assert body["location_name"] == "Main garage"
    assert body["can_take_card_payments"] is True
    assert body["charges_in_currency"] is True
    text = response.get_data(as_text=True)
    for secret in ("EAAA", "EQAA", sq.APPLICATION_SECRET, "_enc"):
        assert secret not in text


def test_status_for_an_unconnected_business(authenticated_client, fake_square):
    body = authenticated_client.get("/api/payments/square/status").get_json()
    assert body["status"] == "NOT_CONNECTED"
    assert body["ready"] is False
    assert body["deployment_configured"] is True


def test_only_eligible_locations_can_be_chosen(authenticated_client, square_garage, fake_square):
    fake_square.locations.append(
        {
            "id": "LOC_US",
            "name": "US",
            "status": "ACTIVE",
            "currency": "USD",
            "capabilities": ["CREDIT_CARD_PROCESSING"],
        }
    )
    fake_square.locations.append(
        {
            "id": "LOC_NOCARD",
            "name": "No cards",
            "status": "ACTIVE",
            "currency": "GBP",
            "capabilities": [],
        }
    )
    for bad in ("LOC_US", "LOC_NOCARD", "LOC_MISSING"):
        response = authenticated_client.put(
            "/api/payments/square/location", json={"location_id": bad}
        )
        assert response.status_code == 422
    response = authenticated_client.put(
        "/api/payments/square/location", json={"location_id": "LOC_GB_1"}
    )
    assert response.status_code == 200
    eligible = {loc["id"]: loc["eligible"] for loc in response.get_json()["locations"]}
    assert eligible == {"LOC_GB_1": True, "LOC_US": False, "LOC_NOCARD": False}


def test_disconnect_revokes_with_square_and_clears_state(
    authenticated_client, square_garage, fake_square, session
):
    response = authenticated_client.post("/api/payments/square/disconnect")
    assert response.status_code == 200
    assert response.get_json() == {"disconnected": True, "remote_revoke_confirmed": True}
    (revoke,) = fake_square.calls("POST", "/oauth2/revoke")
    assert json.loads(revoke.content) == {
        "client_id": sq.APPLICATION_ID,
        "merchant_id": fake_square.merchant_id,
    }
    settings = GaragePaymentSettings.query.filter_by(garage_id=square_garage.id).one()
    assert settings.square_status == "NOT_CONNECTED"
    assert settings.square_access_token_enc is None
    assert settings.square_merchant_id is None


def test_disconnect_is_refused_mid_checkout(authenticated_client, submitted, fake_square):
    response = authenticated_client.post("/api/payments/square/disconnect")
    assert response.status_code == 409
    assert response.get_json()["errors"]["reason"] == "payment_in_progress"


# --- provider selection -----------------------------------------------------


def test_owner_can_choose_square_then_stripe(authenticated_client, square_garage, fake_square):
    body = authenticated_client.put(
        "/api/payments/provider", json={"provider": "square"}
    ).get_json()
    assert body["provider"] == "square"
    assert {o["name"]: o["ready"] for o in body["options"]} == {"stripe": False, "square": True}
    body = authenticated_client.put(
        "/api/payments/provider", json={"provider": "stripe"}
    ).get_json()
    assert body["provider"] == "stripe"


@pytest.mark.parametrize("name", ["fake", "paypal", "venmo"])
def test_unselectable_providers_are_rejected(authenticated_client, name):
    response = authenticated_client.put("/api/payments/provider", json={"provider": name})
    assert response.status_code == 422


def test_switching_is_refused_while_a_payment_is_in_flight(
    authenticated_client, submitted, fake_square
):
    response = authenticated_client.put("/api/payments/provider", json={"provider": "stripe"})
    assert response.status_code == 409
    assert "paying a deposit right now" in response.get_json()["message"]


def test_switching_is_refused_while_a_hold_awaits_payment(
    client, authenticated_client, session, square_garage, fake_square
):
    item = _deposit_type(session, square_garage)
    assert _start_deposit(client, square_garage, item).status_code == 201
    assert (
        authenticated_client.put("/api/payments/provider", json={"provider": "stripe"}).status_code
        == 409
    )


# --- public deposit: two-phase charge ---------------------------------------


def test_deposit_intent_returns_client_safe_square_checkout_data(
    client, session, square_garage, fake_square
):
    item = _deposit_type(session, square_garage)
    response = _start_deposit(client, square_garage, item)
    assert response.status_code == 201
    body = response.get_json()
    assert body["provider"] == "square"
    assert body["checkout_mode"] == "EMBEDDED"
    assert body["payment_status"] == "REQUIRES_PAYMENT"
    assert body["provider_data"] == {
        "application_id": sq.APPLICATION_ID,
        "location_id": "LOC_GB_1",
        "environment": "sandbox",
        "amount_minor": 2000,
        "amount": "20.00",
        "currency": "GBP",
    }
    payment = BookingPayment.query.one()
    assert payment.provider == "square"
    assert payment.provider_account_id == fake_square.merchant_id  # snapshot
    assert payment.provider_payment_id is None
    assert fake_square.calls("POST", "/v2/payments") == []  # nothing charged yet


def test_deposit_intent_is_unavailable_when_square_is_not_ready(
    client, session, garage, fake_square
):
    sq.connect_garage(session, garage, fake_square, location_id=None)
    item = _deposit_type(session, garage)
    response = _start_deposit(client, garage, item)
    assert response.status_code == 503
    assert "aren't available" in response.get_json()["message"]
    assert BookingRequest.query.count() == 0


def test_a_retried_intent_resumes_the_same_uncharged_attempt(
    client, session, square_garage, fake_square
):
    item = _deposit_type(session, square_garage)
    first = _start_deposit(
        client, square_garage, item, payment_attempt_id="7b0c5d0e-6a3c-4f55-9e0a-1d2c3b4a5f60"
    )
    again = _start_deposit(
        client, square_garage, item, payment_attempt_id="7b0c5d0e-6a3c-4f55-9e0a-1d2c3b4a5f60"
    )
    assert again.status_code == 201
    assert again.get_json()["booking_reference"] == first.get_json()["booking_reference"]
    assert again.get_json()["provider_data"]["location_id"] == "LOC_GB_1"
    assert BookingPayment.query.count() == 1


def test_submitting_the_card_charges_once_and_reports_pending(
    submitted, fake_square, client, square_garage
):
    booking_request, payment = submitted
    assert booking_request.status == "AWAITING_PAYMENT"  # webhook decides, not the charge response
    assert payment.status == "PENDING"
    (charge,) = fake_square.calls("POST", "/v2/payments")
    body = json.loads(charge.content)
    assert body["idempotency_key"] == f"deposit-{payment.id}"
    assert len(body["idempotency_key"]) <= 45
    assert body["verification_token"] == "verf:sca-ok"
    assert body["reference_id"] == booking_request.booking_reference

    # Double-submit: the second click returns the same attempt, no new charge.
    again = _submit(
        client, square_garage, booking_request.booking_reference, source_id="cnon:other"
    )
    assert again.status_code == 202
    assert len(fake_square.calls("POST", "/v2/payments")) == 1
    assert BookingPayment.query.count() == 1


def test_the_webhook_confirms_the_booking(client, session, submitted, fake_square):
    booking_request, payment = submitted
    response = _square_webhook(
        client, _payment_event(fake_square, payment.provider_payment_id, "COMPLETED")
    )
    assert response.status_code == 200
    session.refresh(booking_request)
    session.refresh(payment)
    assert payment.status == "SUCCEEDED"
    assert booking_request.status == "PENDING"


def test_poll_reconciles_a_webhook_that_beat_our_commit(
    client, session, submitted, square_garage, fake_square
):
    """Square can deliver payment.updated before the charge request has
    committed its payment id; the webhook then finds nothing. The status poll
    re-checks Square's live status so the customer isn't left waiting."""
    booking_request, _payment = submitted
    poll = client.get(
        f"/api/public/{square_garage.slug}/booking-requests/{booking_request.booking_reference}/payment-status"
    ).get_json()
    assert poll["payment_status"] == "SUCCEEDED"
    assert poll["status"] == "PENDING"


def test_a_declined_card_can_be_retried_with_a_fresh_attempt(
    client, session, square_garage, fake_square
):
    item = _deposit_type(session, square_garage)
    reference = _start_deposit(client, square_garage, item).get_json()["booking_reference"]
    fake_square.decline_next = True
    declined = _submit(client, square_garage, reference, source_id="cnon:declined")
    assert declined.status_code == 400
    assert declined.get_json()["message"] == "Your card was declined."
    booking_request = BookingRequest.query.filter_by(booking_reference=reference).one()
    assert booking_request.status == "AWAITING_PAYMENT"  # slot still held

    retry = _submit(client, square_garage, reference, source_id="cnon:card-ok")
    assert retry.status_code == 202
    payments = BookingPayment.query.order_by(BookingPayment.created_at).all()
    assert [p.status for p in payments] == ["FAILED", "PENDING"]
    keys = [
        json.loads(c.content)["idempotency_key"] for c in fake_square.calls("POST", "/v2/payments")
    ]
    assert keys == [f"deposit-{payments[0].id}", f"deposit-{payments[1].id}"]


def test_submission_after_the_hold_expired_charges_nothing(
    client, session, square_garage, fake_square
):
    item = _deposit_type(session, square_garage)
    reference = _start_deposit(client, square_garage, item).get_json()["booking_reference"]
    booking_request = BookingRequest.query.filter_by(booking_reference=reference).one()
    booking_request.payment_hold_expires_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(minutes=1)
    session.commit()
    response = _submit(client, square_garage, reference)
    assert response.status_code == 409
    assert fake_square.calls("POST", "/v2/payments") == []


def test_token_submission_is_refused_for_a_browser_confirmed_provider(client, session, garage):
    item = _deposit_type(session, garage)  # default fake provider
    reference = _start_deposit(client, garage, item).get_json()["booking_reference"]
    response = _submit(client, garage, reference)
    assert response.status_code == 422
    assert response.get_json()["errors"]["reason"] == "not_token_based"


def test_token_submission_is_tenant_scoped(client, session, submitted, second_garage, fake_square):
    booking_request, _payment = submitted
    response = _submit(client, second_garage, booking_request.booking_reference)
    assert response.status_code == 404


# --- belated success after hold expiry --------------------------------------


def _expire_while_square_still_pending(session, booking_request, fake_square, payment):
    fake_square.payments[payment.provider_payment_id]["status"] = "APPROVED"
    booking_request.payment_hold_expires_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(minutes=1)
    session.commit()
    expire_stale_payment_holds(garage_id=booking_request.garage_id)
    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "EXPIRED"
    assert payment.status == "CANCELLED"


def test_belated_square_success_reinstates_a_still_free_slot(
    client, session, submitted, fake_square
):
    booking_request, payment = submitted
    _expire_while_square_still_pending(session, booking_request, fake_square, payment)

    response = _square_webhook(
        client, _payment_event(fake_square, payment.provider_payment_id, "COMPLETED")
    )
    assert response.status_code == 200
    session.refresh(booking_request)
    session.refresh(payment)
    assert payment.status == "SUCCEEDED"
    assert booking_request.status == "PENDING"
    assert (
        PaymentAuditLog.query.filter_by(action="payment.deposit.reinstated_after_expiry").count()
        == 1
    )


def test_belated_square_success_on_a_taken_slot_is_flagged_for_refund(
    client, session, submitted, fake_square, make_appointment
):
    booking_request, payment = submitted
    _expire_while_square_still_pending(session, booking_request, fake_square, payment)
    start = datetime.datetime.combine(
        booking_request.preferred_date, booking_request.preferred_time, tzinfo=datetime.UTC
    )
    make_appointment(start, minutes=booking_request.appointment_type.default_duration_minutes or 60)

    _square_webhook(client, _payment_event(fake_square, payment.provider_payment_id, "COMPLETED"))
    session.refresh(booking_request)
    session.refresh(payment)
    assert booking_request.status == "EXPIRED"
    assert payment.status == "SUCCEEDED"  # the charge is real
    assert (
        PaymentAuditLog.query.filter_by(action="payment.deposit.succeeded_after_expiry").count()
        == 1
    )


# --- webhooks: tenancy, idempotency, revocation -----------------------------


def test_unknown_merchant_is_recorded_and_acknowledged(client, session, submitted, fake_square):
    _booking_request, payment = submitted
    body = _payment_event(
        fake_square, payment.provider_payment_id, "COMPLETED", merchant_id="OTHER_MERCHANT"
    )
    assert _square_webhook(client, body).status_code == 200
    session.refresh(payment)
    assert payment.status == "PENDING"
    assert PaymentWebhookEvent.query.one().event_type == "unhandled"


def test_another_merchants_event_cannot_touch_this_payment(
    client, session, submitted, second_garage, fake_square
):
    """Garage B is connected to a different merchant; an event signed for
    *B's* merchant naming A's payment id must not settle A's payment."""
    sq.connect_garage(session, second_garage, fake_square, merchant_id="MERCHANT_B")
    _booking_request, payment = submitted
    body = _payment_event(
        fake_square, payment.provider_payment_id, "COMPLETED", merchant_id="MERCHANT_B"
    )
    assert _square_webhook(client, body).status_code == 200
    session.refresh(payment)
    assert payment.status == "PENDING"


def test_duplicate_square_events_are_idempotent(client, session, submitted, fake_square):
    _booking_request, payment = submitted
    body = _payment_event(
        fake_square, payment.provider_payment_id, "COMPLETED", event_id="evt-dup-1"
    )
    assert _square_webhook(client, body).status_code == 200
    assert _square_webhook(client, body).status_code == 200
    assert PaymentWebhookEvent.query.count() == 1
    assert PaymentAuditLog.query.filter_by(action="payment.deposit.succeeded").count() == 1


def test_bad_signature_is_a_400(client, fake_square):
    response = client.post(
        "/api/webhooks/payments/square",
        data=b'{"event_id":"x"}',
        content_type="application/json",
        headers={"x-square-hmacsha256-signature": "nope"},
    )
    assert response.status_code == 400
    assert PaymentWebhookEvent.query.count() == 0


def test_square_webhook_is_503_when_not_configured(client):
    response = client.post("/api/webhooks/payments/square", data=b"{}")
    assert response.status_code == 503


def test_revoked_authorisation_stops_deposits_and_is_audited(
    client, authenticated_client, session, square_garage, fake_square
):
    body = sq.webhook_body(
        "oauth.authorization.revoked", {"revoked_at": "now"}, merchant_id=fake_square.merchant_id
    )
    assert _square_webhook(client, body).status_code == 200
    settings = GaragePaymentSettings.query.filter_by(garage_id=square_garage.id).one()
    assert settings.square_status == "REVOKED"
    assert settings.square_access_token_enc is None
    assert PaymentAuditLog.query.filter_by(action="payment.square.revoked").count() == 1

    item = _deposit_type(session, square_garage)
    assert _start_deposit(client, square_garage, item).status_code == 503
    assert authenticated_client.get("/api/payments/square/status").get_json()["status"] == "REVOKED"


# --- refunds ----------------------------------------------------------------


def _paid(client, session, submitted, fake_square):
    booking_request, payment = submitted
    _square_webhook(client, _payment_event(fake_square, payment.provider_payment_id, "COMPLETED"))
    session.refresh(payment)
    assert payment.status == "SUCCEEDED"
    return booking_request, payment


def test_rejection_refunds_through_square_and_refund_webhook_settles_it(
    client, authenticated_client, session, submitted, fake_square
):
    booking_request, payment = _paid(client, session, submitted, fake_square)
    response = authenticated_client.post(
        f"/api/booking-requests/{booking_request.id}/reject", json={}
    )
    assert response.status_code == 200
    session.refresh(payment)
    assert payment.status == "REFUND_PENDING"
    (refund_call,) = fake_square.calls("POST", "/v2/refunds")
    assert json.loads(refund_call.content)["idempotency_key"] == f"refund-{payment.id}"

    refund_id = payment.provider_refund_id
    body = sq.webhook_body(
        "refund.updated",
        {"id": refund_id, "payment_id": payment.provider_payment_id, "status": "COMPLETED"},
        merchant_id=fake_square.merchant_id,
    )
    assert _square_webhook(client, body).status_code == 200
    session.refresh(payment)
    assert payment.status == "REFUNDED"


def test_refund_after_switching_provider_still_goes_to_square(
    client, authenticated_client, session, submitted, fake_square
):
    booking_request, payment = _paid(client, session, submitted, fake_square)
    assert (
        authenticated_client.put("/api/payments/provider", json={"provider": "stripe"}).status_code
        == 200
    )
    response = authenticated_client.post(
        f"/api/booking-requests/{booking_request.id}/refund", json={"reason": "customer cancelled"}
    )
    assert response.status_code == 200
    session.refresh(payment)
    assert payment.provider == "square"
    assert payment.status == "REFUND_PENDING"
    assert len(fake_square.calls("POST", "/v2/refunds")) == 1


def test_refund_after_disconnect_fails_clearly(
    client, authenticated_client, session, submitted, fake_square
):
    booking_request, payment = _paid(client, session, submitted, fake_square)
    assert authenticated_client.post("/api/payments/square/disconnect").status_code == 200
    refund_deposit(booking_request, reason="test")
    session.refresh(payment)
    assert payment.status == "REFUND_FAILED"
    assert "isn't connected" in payment.refund_failure_reason


# --- Platform Admin / secrets in logs ---------------------------------------


def test_platform_admin_sees_square_prerequisites_and_readiness(
    platform_client, session, square_garage, fake_square
):
    settings = square_garage.payment_settings
    settings.provider = "square"
    session.commit()
    readiness = platform_client.get(
        f"/api/platform-admin/tenants/{square_garage.id}/readiness"
    ).get_json()
    checks = {c["key"]: c["ok"] for c in readiness["payments"]}
    assert checks["square_connected"] is True
    assert checks["square_location_selected"] is True
    assert checks["deposit_configuration_valid"] is True

    sq.unconfigure_square(platform_client._client.application)
    from app.payments.config import payments_prerequisites

    keys = {p["key"]: p["satisfied"] for p in payments_prerequisites()}
    assert keys["square_application_id"] is False
    assert keys["square_webhook_signature_key"] is False
    assert keys["payments_secret_key"] is True


def test_no_token_or_secret_reaches_the_logs(client, session, garage, user, fake_square, caplog):
    caplog.set_level(logging.DEBUG)
    client.get(
        "/api/payments/square/oauth/callback",
        query_string={"code": "good-code", "state": _state_for(garage, user)},
    )
    fake_square.refresh_result = "revoked"
    settings = garage.payment_settings
    settings.square_token_expires_at = datetime.datetime.now(datetime.UTC)
    session.commit()
    with pytest.raises(square_oauth.SquareNotConnected):
        square_oauth.access_token_for_merchant(fake_square.merchant_id)
    for secret in ("EAAA", "EQAA", sq.APPLICATION_SECRET, sq.SIGNATURE_KEY):
        assert secret not in caplog.text
