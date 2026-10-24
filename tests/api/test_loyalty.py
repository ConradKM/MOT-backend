"""Loyalty rewards and discount codes (app/loyalty/).

Covers the milestone maths and exactly-once issuing, the reconcile command,
the reward email and unsubscribe, ``evaluate_code`` for every outcome, the
public checkout (validate, plain and deposit bookings, the zero-total path,
idempotency, the double-redemption race), the redemption lifecycle, staff
create/update and re-pricing, and the owner-only management API.
"""

from __future__ import annotations

import datetime
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.loyalty import issuing
from app.loyalty.issuing import issue_reward_for_completion, reconcile
from app.loyalty.service import (
    CUSTOMER_LIMIT,
    DISABLED,
    EXPIRED,
    NOT_FOUND,
    NOT_YET_VALID,
    NOTHING_TO_DISCOUNT,
    UNPRICED,
    USED_UP,
    WRONG_CUSTOMER,
    DiscountFailure,
    DiscountResult,
    compute_discount,
    evaluate_code,
)
from app.loyalty.unsubscribe import make_token
from app.models.appointments.add_on import AddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.loyalty.discount_code import DiscountCode
from app.models.loyalty.discount_redemption import DiscountRedemption
from app.models.loyalty.loyalty_settings import LoyaltySettings
from app.payments.service import expire_stale_payment_holds
from tests.conftest import DEFAULT_PASSWORD, AuthenticatedClient

NOW = datetime.datetime.now(datetime.UTC)


def _future_weekday(days_ahead=7):
    d = NOW.date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday().isoformat()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def priced_type(session, garage):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="Full service",
        status="ACTIVE",
        default_duration_minutes=60,
        base_price=Decimal("100.00"),
    )
    session.add(t)
    session.commit()
    return t


@pytest.fixture()
def deposit_type(session, garage):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="MOT",
        status="ACTIVE",
        default_duration_minutes=60,
        base_price=Decimal("100.00"),
        deposit_required=True,
        deposit_type="PERCENTAGE",
        deposit_value=Decimal("20.00"),
    )
    session.add(t)
    session.commit()
    return t


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


def _enable(session, garage, *, visits=3, discount_type="PERCENTAGE", value="10.00", days=30):
    settings = LoyaltySettings(
        garage_id=garage.id,
        enabled=True,
        visits_required=visits,
        discount_type=discount_type,
        discount_value=Decimal(value),
        expiry_days=days,
        enabled_at=NOW - datetime.timedelta(days=1),
    )
    session.add(settings)
    session.commit()
    return settings


def _code(session, garage, text="SUMMER10", **overrides):
    fields = {
        "garage_id": garage.id,
        "code": text,
        "code_normalized": text.replace("-", "").upper(),
        "source": "MANUAL",
        "discount_type": "PERCENTAGE",
        "discount_value": Decimal("10.00"),
        "status": "ACTIVE",
    }
    fields.update(overrides)
    code = DiscountCode(**fields)
    session.add(code)
    session.commit()
    return code


def _loyalty_code(session, garage, customer, text="LOYAL-ABCDEFGH", visit=3, **overrides):
    return _code(
        session,
        garage,
        text,
        source="LOYALTY",
        customer_id=customer.id,
        max_uses=1,
        max_uses_per_customer=1,
        issued_for_visit_count=visit,
        expires_at=NOW + datetime.timedelta(days=30),
        **overrides,
    )


def _appointment(session, garage, user, customer, appt_type, *, status="BOOKED", days=-1, **kw):
    start = NOW + datetime.timedelta(days=days)
    appt = Appointment(
        garage_id=garage.id,
        employee_id=user.id,
        customer_id=customer.id,
        appointment_type_id=appt_type.id,
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        status=status,
        price_at_booking=appt_type.base_price,
        **kw,
    )
    session.add(appt)
    session.commit()
    return appt


def _complete(client, appointment):
    return client.patch(f"/api/appointments/{appointment.id}", json={"status": "COMPLETED"})


def _codes_for(customer):
    return (
        DiscountCode.query.filter_by(customer_id=customer.id)
        .order_by(DiscountCode.issued_for_visit_count)
        .all()
    )


def _payload(appt_type, **overrides):
    payload = {
        "customer_first_name": "Jane",
        "customer_last_name": "Doe",
        "customer_email": "jane.doe@example.com",
        "customer_phone": "07123 456789",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
        "appointment_type_id": str(appt_type.id),
    }
    payload.update(overrides)
    return payload


def _submit(client, garage, appt_type, **overrides):
    return client.post(
        f"/api/public/{garage.slug}/booking-requests", json=_payload(appt_type, **overrides)
    )


def _deposit_intent(client, garage, appt_type, **overrides):
    return client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_payload(appt_type, **overrides),
    )


def _validate(client, garage, appt_type, code, email="jane.doe@example.com", add_ons=None):
    return client.post(
        f"/api/public/{garage.slug}/discount-codes/validate",
        json={
            "code": code,
            "appointment_type_id": str(appt_type.id),
            "customer_email": email,
            "add_ons": add_ons or [],
        },
    )


def _webhook_success(client, provider_payment_id):
    return client.post(
        "/api/webhooks/payments/fake",
        data=json.dumps(
            {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "payment.succeeded",
                "provider_payment_id": provider_payment_id,
            }
        ),
        content_type="application/json",
        headers={"Fake-Signature": "valid"},
    )


def _redemption(code):
    return (
        DiscountRedemption.query.filter_by(discount_code_id=code.id)
        .order_by(DiscountRedemption.created_at.desc())
        .first()
    )


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "kind", "value", "expected"),
    [
        ("100.00", "PERCENTAGE", "10", "10.00"),
        ("99.99", "PERCENTAGE", "12.5", "12.50"),  # 12.49875 rounds half-up
        ("10.05", "PERCENTAGE", "50", "5.03"),  # 5.025 -> 5.03, not banker's 5.02
        ("30.00", "FIXED", "50.00", "30.00"),  # capped at the total
        ("30.00", "FIXED", "5.00", "5.00"),
        ("45.00", "PERCENTAGE", "100", "45.00"),
    ],
)
def test_discount_rounds_half_up_and_never_exceeds_the_total(total, kind, value, expected):
    assert compute_discount(Decimal(total), kind, Decimal(value)) == Decimal(expected)


# --------------------------------------------------------------------------
# evaluate_code
# --------------------------------------------------------------------------


def _eval(garage, code, customer=None, total="100.00"):
    return evaluate_code(
        garage, code, customer=customer, total=None if total is None else Decimal(total)
    )


def test_evaluate_applies_a_promo_code_with_normalised_input(session, garage):
    _code(session, garage, "SUMMER-10")
    result = _eval(garage, "  summer 10 ")
    assert isinstance(result, DiscountResult)
    assert result.discount_amount == Decimal("10.00")
    assert result.new_total == Decimal("90.00")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": "DISABLED"}, DISABLED),
        ({"valid_from": NOW + datetime.timedelta(days=1)}, NOT_YET_VALID),
        ({"expires_at": NOW - datetime.timedelta(minutes=1)}, EXPIRED),
    ],
)
def test_evaluate_rejects_inactive_codes(session, garage, overrides, reason):
    _code(session, garage, **overrides)
    result = _eval(garage, "SUMMER10")
    assert isinstance(result, DiscountFailure) and result.reason == reason


def test_evaluate_rejects_unknown_and_other_tenants_codes(session, garage, second_garage):
    _code(session, second_garage, "THEIRS10")
    assert _eval(garage, "THEIRS10").reason == NOT_FOUND
    assert _eval(garage, "nope").reason == NOT_FOUND


def test_evaluate_needs_a_price_before_revealing_anything(session, garage):
    _code(session, garage)
    assert _eval(garage, "SUMMER10", total=None).reason == UNPRICED
    assert _eval(garage, "NOT-A-CODE", total=None).reason == UNPRICED
    assert _eval(garage, "SUMMER10", total="0.00").reason == NOTHING_TO_DISCOUNT


def test_loyalty_code_only_applies_to_its_customer(session, garage, customer):
    other = Customer(garage_id=garage.id, first_name="Other", last_name="Person")
    session.add(other)
    session.commit()
    _loyalty_code(session, garage, customer)
    assert isinstance(_eval(garage, "LOYAL-ABCDEFGH", customer), DiscountResult)
    assert _eval(garage, "LOYAL-ABCDEFGH", other).reason == WRONG_CUSTOMER
    assert _eval(garage, "LOYAL-ABCDEFGH", None).reason == WRONG_CUSTOMER


def test_use_limits(session, garage, customer, booking_request):
    code = _code(session, garage, max_uses=2, max_uses_per_customer=1)
    session.add(
        DiscountRedemption(
            garage_id=garage.id,
            discount_code_id=code.id,
            booking_request_id=booking_request.id,
            customer_id=customer.id,
            amount=Decimal("10.00"),
            status="CONSUMED",
        )
    )
    session.commit()
    assert _eval(garage, "SUMMER10", customer).reason == CUSTOMER_LIMIT
    assert isinstance(_eval(garage, "SUMMER10", None), DiscountResult)

    session.add(
        DiscountRedemption(
            garage_id=garage.id,
            discount_code_id=code.id,
            booking_request_id=booking_request.id,
            amount=Decimal("10.00"),
            status="RESERVED",
        )
    )
    session.commit()
    assert _eval(garage, "SUMMER10", None).reason == USED_UP


def test_a_redemption_whose_booking_fell_through_no_longer_counts(
    session, garage, customer, booking_request
):
    code = _code(session, garage, max_uses=1)
    session.add(
        DiscountRedemption(
            garage_id=garage.id,
            discount_code_id=code.id,
            booking_request_id=booking_request.id,
            amount=Decimal("10.00"),
            status="CONSUMED",
            single_use=True,
        )
    )
    booking_request.status = "REJECTED"
    session.commit()
    assert isinstance(_eval(garage, "SUMMER10", customer), DiscountResult)
    assert _redemption(code).status == "RELEASED"


# --------------------------------------------------------------------------
# Issuing
# --------------------------------------------------------------------------


def test_every_visit_issues_when_x_is_one(
    authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=1, days=14)
    appt = _appointment(session, garage, authenticated_user.user, customer, priced_type)

    assert _complete(authenticated_user.client, appt).status_code == 200

    (code,) = _codes_for(customer)
    assert code.source == "LOYALTY"
    assert code.code.startswith("LOYAL-")
    assert code.max_uses == 1
    assert code.issued_for_visit_count == 1
    assert code.issued_for_appointment_id == appt.id
    assert abs(code.expires_at - (NOW + datetime.timedelta(days=14))) < datetime.timedelta(
        minutes=5
    )
    assert code.email_status == "SENT"
    log = CommunicationLog.query.filter_by(discount_code_id=code.id).one()
    assert log.trigger_event == "LOYALTY_REWARD_ISSUED"
    assert code.code in log.body
    assert "/unsubscribe?token=" in log.body
    assert f"/book/{garage.id}" in log.body


def test_every_xth_visit_issues_and_the_count_keeps_going(
    authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=3)
    for i in range(6):
        appt = _appointment(
            session, garage, authenticated_user.user, customer, priced_type, days=-10 + i
        )
        assert _complete(authenticated_user.client, appt).status_code == 200
        assert len(_codes_for(customer)) == (i + 1) // 3

    assert [c.issued_for_visit_count for c in _codes_for(customer)] == [3, 6]


def test_history_counts_but_enabling_never_mass_issues(
    authenticated_user, session, garage, customer, priced_type
):
    # Twelve visits from before the programme (and before completed_at).
    for i in range(12):
        _appointment(
            session,
            garage,
            authenticated_user.user,
            customer,
            priced_type,
            status="COMPLETED",
            days=-100 + i,
        )
    resp = authenticated_user.client.put(
        "/api/loyalty/settings",
        json={
            "enabled": True,
            "visits_required": 5,
            "discount_type": "PERCENTAGE",
            "discount_value": "10.00",
            "expiry_days": 30,
        },
    )
    assert resp.status_code == 200
    assert reconcile() == []
    assert _codes_for(customer) == []

    # Visits 13 and 14 earn nothing; visit 15 lands on the milestone.
    for i in range(3):
        appt = _appointment(
            session, garage, authenticated_user.user, customer, priced_type, days=-5 + i
        )
        _complete(authenticated_user.client, appt)
    assert [c.issued_for_visit_count for c in _codes_for(customer)] == [15]


def test_a_disabled_programme_issues_nothing(
    authenticated_user, session, garage, customer, priced_type
):
    settings = _enable(session, garage, visits=1)
    settings.enabled = False
    session.commit()
    appt = _appointment(session, garage, authenticated_user.user, customer, priced_type)
    _complete(authenticated_user.client, appt)
    assert _codes_for(customer) == []


def test_issuing_is_exactly_once_across_retries_and_status_flaps(
    authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=1)
    appt = _appointment(session, garage, authenticated_user.user, customer, priced_type)
    _complete(authenticated_user.client, appt)

    # A retried call, and a completed -> reopened -> completed flap.
    issue_reward_for_completion(appt)
    appt.status = "IN_PROGRESS"
    appt.completed_at = None
    session.commit()
    appt.status = "COMPLETED"
    appt.completed_at = datetime.datetime.now(datetime.UTC)
    session.commit()
    issue_reward_for_completion(appt)
    reconcile()

    assert len(_codes_for(customer)) == 1
    assert CommunicationLog.query.filter_by(trigger_event="LOYALTY_REWARD_ISSUED").count() == 1


def test_concurrent_completions_each_land_on_their_own_visit(
    app, authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=1)
    first = _appointment(session, garage, authenticated_user.user, customer, priced_type, days=-3)
    second = _appointment(session, garage, authenticated_user.user, customer, priced_type, days=-2)
    ids = [first.id, second.id]
    token = authenticated_user.access_token
    barrier = Barrier(2)

    def complete(appointment_id):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            return client.patch(
                f"/api/appointments/{appointment_id}",
                json={"status": "COMPLETED"},
                headers={"Authorization": f"Bearer {token}"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(complete, ids)) == [200, 200]

    db.session.expire_all()
    assert sorted(c.issued_for_visit_count for c in _codes_for(customer)) == [1, 2]


def test_opted_out_and_emailless_customers_get_a_code_but_no_email(
    authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=1)
    customer.marketing_email_opt_out = True
    no_email = Customer(garage_id=garage.id, first_name="No", last_name="Email")
    session.add(no_email)
    session.commit()

    _complete(
        authenticated_user.client,
        _appointment(session, garage, authenticated_user.user, customer, priced_type),
    )
    _complete(
        authenticated_user.client,
        _appointment(session, garage, authenticated_user.user, no_email, priced_type),
    )

    assert _codes_for(customer)[0].email_status == "OPTED_OUT"
    assert _codes_for(no_email)[0].email_status == "NO_EMAIL"
    assert CommunicationLog.query.filter_by(trigger_event="LOYALTY_REWARD_ISSUED").count() == 0


def test_imported_history_counts_but_never_triggers(
    authenticated_user, session, garage, customer, priced_type
):
    _enable(session, garage, visits=2)
    _appointment(
        session,
        garage,
        authenticated_user.user,
        customer,
        priced_type,
        status="COMPLETED",
        imported_at=NOW,
    )
    _appointment(
        session,
        garage,
        authenticated_user.user,
        customer,
        priced_type,
        status="COMPLETED",
        imported_at=NOW,
    )
    assert reconcile() == []
    assert _codes_for(customer) == []

    # Visits 3 and 4 (live) - the 4th lands on a milestone.
    for days in (-2, -1):
        _complete(
            authenticated_user.client,
            _appointment(
                session, garage, authenticated_user.user, customer, priced_type, days=days
            ),
        )
    assert [c.issued_for_visit_count for c in _codes_for(customer)] == [4]


def test_bulk_import_never_marks_a_live_completion():
    """Imports count toward visits but can't trigger, because they never set
    completed_at - guard that the importer keeps it that way."""
    import inspect

    from app.bulk_import import appointments as importer

    assert "completed_at" not in inspect.getsource(importer)


def test_a_loyalty_failure_never_blocks_completion_and_reconcile_catches_up(
    authenticated_user, session, garage, customer, priced_type, monkeypatch
):
    _enable(session, garage, visits=1)
    appt = _appointment(session, garage, authenticated_user.user, customer, priced_type)

    def boom(*args, **kwargs):
        raise RuntimeError("loyalty bug")

    monkeypatch.setattr(issuing, "_issue", boom)
    resp = _complete(authenticated_user.client, appt)
    assert resp.status_code == 200
    assert Appointment.query.get(appt.id).status == "COMPLETED"
    assert _codes_for(customer) == []

    monkeypatch.undo()
    issued = reconcile()
    assert [c.issued_for_visit_count for c in issued] == [1]
    assert _codes_for(customer)[0].email_status == "SENT"


def test_reconcile_cli(app, authenticated_user, session, garage, customer, priced_type):
    _enable(session, garage, visits=1)
    _appointment(
        session,
        garage,
        authenticated_user.user,
        customer,
        priced_type,
        status="COMPLETED",
        completed_at=NOW - datetime.timedelta(hours=1),
    )
    result = app.test_cli_runner().invoke(args=["loyalty-reconcile"])
    assert result.exit_code == 0
    assert "issued 1 code(s)" in result.output
    assert "LOYAL-" not in result.output  # never print the code itself


# --------------------------------------------------------------------------
# Public checkout
# --------------------------------------------------------------------------


def test_validate_returns_the_breakdown_and_recalculated_deposit(
    client, session, garage, deposit_type
):
    _code(session, garage, "SAVE25", discount_type="FIXED", discount_value=Decimal("25.00"))
    resp = _validate(client, garage, deposit_type, "save-25")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == "SAVE25"
    assert body["original_total"] == "100.00"
    assert body["discount_amount"] == "25.00"
    assert body["new_total"] == "75.00"
    assert body["deposit_required"] is True
    assert body["deposit_amount"] == "15.00"  # 20% of the discounted 75.00
    assert body["remaining_balance"] == "60.00"


def test_validate_includes_add_ons_in_the_total(client, session, garage, priced_type):
    add_on = AddOn(
        garage_id=garage.id,
        appointment_type_id=priced_type.id,
        name="Wash",
        price_delta=Decimal("20.00"),
        duration_delta_minutes=0,
        status="ACTIVE",
    )
    session.add(add_on)
    session.commit()
    _code(session, garage)
    body = _validate(
        client, garage, priced_type, "SUMMER10", add_ons=[{"add_on_id": str(add_on.id)}]
    ).get_json()
    assert body["original_total"] == "120.00"
    assert body["discount_amount"] == "12.00"


def test_validate_gives_one_generic_error_for_every_bad_code(
    client, session, garage, customer, priced_type
):
    _code(session, garage, "OLDCODE", expires_at=NOW - datetime.timedelta(days=1))
    _code(session, garage, "OFFCODE", status="DISABLED")
    _loyalty_code(session, garage, customer)
    responses = [
        _validate(client, garage, priced_type, "DOESNOTEXIST"),
        _validate(client, garage, priced_type, "OLDCODE"),
        _validate(client, garage, priced_type, "OFFCODE"),
        _validate(client, garage, priced_type, "LOYAL-ABCDEFGH", email="someone@else.example"),
    ]
    bodies = [r.get_json() for r in responses]
    assert {r.status_code for r in responses} == {422}
    assert len({b["message"] for b in bodies}) == 1
    assert {b["errors"]["reason"] for b in bodies} == {"discount_code_invalid"}
    # ...while the rightful customer can use their loyalty code.
    assert _validate(client, garage, priced_type, "LOYAL-ABCDEFGH").status_code == 200


def test_validate_explains_an_unpriced_service(client, session, garage, appointment_type):
    _code(session, garage)
    body = _validate(client, garage, appointment_type, "SUMMER10").get_json()
    assert "price" in body["message"]


def test_validate_is_tenant_scoped(client, session, garage, second_garage, priced_type):
    _code(session, garage, "OURS10")
    other_type = GarageAppointmentType(
        garage_id=second_garage.id, name="X", status="ACTIVE", base_price=Decimal("50.00")
    )
    session.add(other_type)
    session.commit()
    assert _validate(client, second_garage, other_type, "OURS10").status_code == 422


def test_validate_is_rate_limited_per_ip_and_per_business(app):
    from app.public_booking.routes import DiscountCodeValidate

    # End-to-end 429s can't run in this suite (RATELIMIT_ENABLED=False - see
    # the NOTE at the end of test_public_booking.py); assert the wiring.
    assert app.config["PUBLIC_DISCOUNT_VALIDATE_RATELIMIT"]
    assert app.config["PUBLIC_DISCOUNT_VALIDATE_GARAGE_RATELIMIT"]
    assert DiscountCodeValidate.post.__wrapped__ is not None


def test_plain_booking_with_a_code_is_discounted_and_consumes_it(
    client, session, garage, customer, priced_type
):
    code = _loyalty_code(session, garage, customer)
    resp = _submit(client, garage, priced_type, discount_code="loyal abcdefgh")
    assert resp.status_code == 201

    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking.requested_price == Decimal("90.00")
    assert booking.price_before_discount == Decimal("100.00")
    assert booking.discount_amount == Decimal("10.00")
    assert booking.discount_code == "LOYAL-ABCDEFGH"
    assert _redemption(code).status == "CONSUMED"

    # Single-use: a second booking can't use it again.
    again = _submit(
        client, garage, priced_type, discount_code="LOYAL-ABCDEFGH", preferred_time="11:00:00"
    )
    assert again.status_code == 422
    assert again.get_json()["errors"]["reason"] == "discount_code_invalid"


def test_bookings_without_a_code_are_unchanged(client, session, garage, priced_type):
    assert _submit(client, garage, priced_type).status_code == 201
    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking.requested_price == Decimal("100.00")
    assert booking.discount_code_id is None and booking.price_before_discount is None


def test_deposit_is_taken_on_the_discounted_total_and_consumed_on_payment(
    client, session, garage, deposit_type
):
    code = _code(session, garage, "HALF", discount_value=Decimal("50.00"))
    resp = _deposit_intent(client, garage, deposit_type, discount_code="HALF")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["service_total"] == "50.00"
    assert body["deposit_amount"] == "10.00"  # 20% of 50, not of 100
    assert body["discount_code"] == "HALF"
    assert body["price_before_discount"] == "100.00"
    assert _redemption(code).status == "RESERVED"

    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert _webhook_success(client, booking.active_payment.provider_payment_id).status_code == 200
    assert BookingRequest.query.get(booking.id).status == "PENDING"
    assert _redemption(code).status == "CONSUMED"


def test_hold_expiry_releases_a_reserved_code(client, session, garage, customer, deposit_type):
    code = _loyalty_code(session, garage, customer)
    assert (
        _deposit_intent(client, garage, deposit_type, discount_code="LOYAL-ABCDEFGH").status_code
        == 201
    )
    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    booking.payment_hold_expires_at = NOW - datetime.timedelta(minutes=1)
    session.commit()

    expire_stale_payment_holds(garage_id=garage.id)

    assert BookingRequest.query.get(booking.id).status == "EXPIRED"
    assert _redemption(code).status == "RELEASED"
    assert _validate(client, garage, deposit_type, "LOYAL-ABCDEFGH").status_code == 200


def test_rejection_releases_the_code(
    authenticated_user, client, session, garage, customer, priced_type
):
    code = _loyalty_code(session, garage, customer)
    _submit(client, garage, priced_type, discount_code="LOYAL-ABCDEFGH")
    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    resp = authenticated_user.client.post(f"/api/booking-requests/{booking.id}/reject", json={})
    assert resp.status_code == 200
    assert _redemption(code).status == "RELEASED"


def test_approval_carries_the_discount_and_cancelling_gives_the_code_back(
    authenticated_user, client, session, garage, customer, priced_type
):
    code = _loyalty_code(session, garage, customer)
    _submit(client, garage, priced_type, discount_code="LOYAL-ABCDEFGH")
    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    resp = authenticated_user.client.post(
        f"/api/booking-requests/{booking.id}/approve",
        json={"employee_id": str(authenticated_user.user.id)},
    )
    assert resp.status_code == 200, resp.get_json()

    appt = Appointment.query.filter_by(garage_id=garage.id).one()
    assert appt.price_at_booking == Decimal("90.00")
    assert appt.price_before_discount == Decimal("100.00")
    assert appt.discount_code == "LOYAL-ABCDEFGH"
    redemption = _redemption(code)
    assert redemption.appointment_id == appt.id and redemption.status == "CONSUMED"

    authenticated_user.client.delete(f"/api/appointments/{appt.id}")
    assert _redemption(code).status == "RELEASED"


def test_a_fully_discounted_deposit_booking_skips_the_deposit(
    client, session, garage, deposit_type
):
    _code(session, garage, "FREE", discount_value=Decimal("100.00"))

    intent = _deposit_intent(client, garage, deposit_type, discount_code="FREE")
    assert intent.status_code == 422
    assert intent.get_json()["errors"]["reason"] == "deposit_not_required"

    plain = _submit(client, garage, deposit_type, discount_code="FREE")
    assert plain.status_code == 201
    booking = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert booking.status in ("PENDING", "APPROVED")
    assert booking.requested_price == Decimal("0.00")
    assert booking.payments == []


def test_a_partly_discounted_deposit_booking_still_needs_the_deposit(
    client, session, garage, deposit_type
):
    _code(session, garage)
    resp = _submit(client, garage, deposit_type, discount_code="SUMMER10")
    assert resp.status_code == 422
    assert "deposit" in resp.get_json()["message"]
    resp = _submit(client, garage, deposit_type)
    assert resp.status_code == 422


def test_retries_are_idempotent_and_bound_to_the_code(client, session, garage, priced_type):
    code = _code(session, garage, max_uses=5)
    attempt = str(uuid.uuid4())
    first = _submit(
        client, garage, priced_type, discount_code="SUMMER10", payment_attempt_id=attempt
    )
    retry = _submit(
        client, garage, priced_type, discount_code="summer-10", payment_attempt_id=attempt
    )
    assert first.status_code == retry.status_code == 201
    assert first.get_json()["id"] == retry.get_json()["id"]
    assert DiscountRedemption.query.filter_by(discount_code_id=code.id).count() == 1

    other = _submit(client, garage, priced_type, payment_attempt_id=attempt)
    assert other.status_code == 409


def test_deposit_retries_are_bound_to_the_code(client, session, garage, deposit_type):
    _code(session, garage, max_uses=5)
    attempt = str(uuid.uuid4())
    assert (
        _deposit_intent(client, garage, deposit_type, payment_attempt_id=attempt).status_code == 201
    )
    mismatch = _deposit_intent(
        client, garage, deposit_type, discount_code="SUMMER10", payment_attempt_id=attempt
    )
    assert mismatch.status_code == 409


def test_no_token_duplicate_fallback_does_not_swallow_a_different_code(
    client, session, garage, priced_type
):
    _code(session, garage, max_uses=5)
    first = _submit(client, garage, priced_type, preferred_time=None)
    second = _submit(client, garage, priced_type, preferred_time=None, discount_code="SUMMER10")
    assert first.status_code == second.status_code == 201
    assert first.get_json()["id"] != second.get_json()["id"]


def test_two_simultaneous_checkouts_cannot_both_use_a_single_use_code(
    app, session, garage, customer, priced_type
):
    _loyalty_code(session, garage, customer)
    url = f"/api/public/{garage.slug}/booking-requests"
    payloads = [
        _payload(priced_type, discount_code="LOYAL-ABCDEFGH", preferred_time=time)
        for time in ("09:30:00", "13:00:00")
    ]
    barrier = Barrier(2)

    def submit(payload):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            return client.post(url, json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, payloads))

    assert statuses == [201, 422]
    db.session.expire_all()
    assert DiscountRedemption.query.filter(DiscountRedemption.status != "RELEASED").count() == 1


# --------------------------------------------------------------------------
# Staff appointments
# --------------------------------------------------------------------------


def _create(client, user, customer, appt_type, **extra):
    return client.post(
        "/api/appointments/",
        json={
            "employee_id": str(user.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": (NOW + datetime.timedelta(days=2)).isoformat(),
            **extra,
        },
    )


def test_staff_create_applies_a_code(authenticated_user, session, garage, customer, priced_type):
    code = _loyalty_code(session, garage, customer)
    resp = _create(
        authenticated_user.client,
        authenticated_user.user,
        customer,
        priced_type,
        discount_code="LOYAL-ABCDEFGH",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["price_at_booking"] == "90.00"
    assert body["price_before_discount"] == "100.00"
    assert body["discount_amount"] == "10.00"
    assert body["discount_code"] == "LOYAL-ABCDEFGH"
    assert _redemption(code).status == "CONSUMED"


def test_staff_get_specific_reasons(
    authenticated_user, session, garage, customer, priced_type, appointment_type
):
    other = Customer(garage_id=garage.id, first_name="Other", last_name="Person")
    session.add(other)
    session.commit()
    _loyalty_code(session, garage, customer)
    _code(session, garage)

    wrong = _create(
        authenticated_user.client,
        authenticated_user.user,
        other,
        priced_type,
        discount_code="LOYAL-ABCDEFGH",
    )
    assert wrong.status_code == 422
    assert "different customer" in wrong.get_json()["message"]
    assert wrong.get_json()["errors"]["json"]["discount_code"]

    unpriced = _create(
        authenticated_user.client,
        authenticated_user.user,
        customer,
        appointment_type,
        discount_code="SUMMER10",
    )
    assert unpriced.status_code == 422
    assert "price" in unpriced.get_json()["message"]


def test_editing_a_discounted_appointment_neither_undoes_nor_compounds_it(
    authenticated_user, session, garage, customer, priced_type
):
    add_on = AddOn(
        garage_id=garage.id,
        appointment_type_id=priced_type.id,
        name="Wash",
        price_delta=Decimal("20.00"),
        duration_delta_minutes=0,
        status="ACTIVE",
    )
    session.add(add_on)
    session.commit()
    _code(session, garage, max_uses=10)
    _code(session, garage, "FIVER", discount_type="FIXED", discount_value=Decimal("5.00"))
    created = _create(
        authenticated_user.client,
        authenticated_user.user,
        customer,
        priced_type,
        discount_code="SUMMER10",
    ).get_json()
    url = f"/api/appointments/{created['id']}"
    client = authenticated_user.client

    # Re-saving as the form does (same type, same code) changes nothing.
    body = client.patch(
        url,
        json={
            "appointment_type_id": str(priced_type.id),
            "discount_code": "SUMMER10",
            "notes": "x",
        },
    ).get_json()
    assert body["price_at_booking"] == "90.00"

    # Adding an add-on re-applies the frozen 10% to the new total, once.
    body = client.patch(url, json={"add_ons": [{"add_on_id": str(add_on.id)}]}).get_json()
    assert body["price_before_discount"] == "120.00"
    assert body["discount_amount"] == "12.00"
    assert body["price_at_booking"] == "108.00"

    # ...and removing it again goes back, not further.
    body = client.patch(url, json={"add_ons": []}).get_json()
    assert body["price_at_booking"] == "90.00"

    # Replacing the code releases the old one.
    body = client.patch(url, json={"discount_code": "FIVER"}).get_json()
    assert body["discount_code"] == "FIVER"
    assert body["price_at_booking"] == "95.00"

    # Removing it restores the full price.
    body = client.patch(url, json={"discount_code": None}).get_json()
    assert body["discount_code"] is None
    assert body["price_at_booking"] == "100.00"
    assert DiscountRedemption.query.filter_by(status="CONSUMED").count() == 0


def test_a_loyalty_code_blocks_moving_the_appointment_to_another_customer(
    authenticated_user, session, garage, customer, priced_type
):
    other = Customer(garage_id=garage.id, first_name="Other", last_name="Person")
    session.add(other)
    session.commit()
    _loyalty_code(session, garage, customer)
    created = _create(
        authenticated_user.client,
        authenticated_user.user,
        customer,
        priced_type,
        discount_code="LOYAL-ABCDEFGH",
    ).get_json()
    resp = authenticated_user.client.patch(
        f"/api/appointments/{created['id']}", json={"customer_id": str(other.id)}
    )
    assert resp.status_code == 422


def test_staff_preview_endpoint(authenticated_user, session, garage, customer, priced_type):
    _code(session, garage)
    resp = authenticated_user.client.post(
        "/api/loyalty/evaluate",
        json={
            "code": "summer10",
            "customer_id": str(customer.id),
            "appointment_type_id": str(priced_type.id),
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["new_total"] == "90.00"
    assert DiscountRedemption.query.count() == 0


# --------------------------------------------------------------------------
# Management API
# --------------------------------------------------------------------------


def test_settings_defaults_update_and_validation(authenticated_user, staff_client):
    client = authenticated_user.client
    body = client.get("/api/loyalty/settings").get_json()
    assert body["enabled"] is False and body["visits_required"] == 5

    good = {
        "enabled": True,
        "visits_required": 4,
        "discount_type": "FIXED",
        "discount_value": "7.50",
        "expiry_days": 60,
    }
    resp = client.put("/api/loyalty/settings", json=good)
    assert resp.status_code == 200
    assert resp.get_json()["enabled_at"] is not None

    assert (
        client.put("/api/loyalty/settings", json={**good, "visits_required": 0}).status_code == 422
    )
    bad_percent = {**good, "discount_type": "PERCENTAGE", "discount_value": "120"}
    assert client.put("/api/loyalty/settings", json=bad_percent).status_code == 422
    assert (
        client.put("/api/loyalty/settings", json={**good, "discount_value": "0"}).status_code == 422
    )

    assert staff_client.get("/api/loyalty/settings").status_code == 200
    assert staff_client.put("/api/loyalty/settings", json=good).status_code == 403


def test_create_codes_typed_and_generated(authenticated_user, staff_client):
    client = authenticated_user.client
    resp = client.post(
        "/api/loyalty/codes",
        json={"code": "summer-10", "discount_type": "PERCENTAGE", "discount_value": "10"},
    )
    assert resp.status_code == 201
    assert resp.get_json()["code"] == "SUMMER-10"
    assert resp.get_json()["status"] == "ACTIVE"

    dup = client.post(
        "/api/loyalty/codes",
        json={"code": "Summer 10", "discount_type": "FIXED", "discount_value": "5"},
    )
    assert dup.status_code == 409

    generated = client.post(
        "/api/loyalty/codes",
        json={"discount_type": "FIXED", "discount_value": "5.00", "max_uses": 100},
    ).get_json()
    assert len(generated["code"]) == 8
    assert not set(generated["code"]) & set("01OI")

    bad_window = client.post(
        "/api/loyalty/codes",
        json={
            "discount_type": "FIXED",
            "discount_value": "5",
            "valid_from": (NOW + datetime.timedelta(days=2)).isoformat(),
            "expires_at": (NOW + datetime.timedelta(days=1)).isoformat(),
        },
    )
    assert bad_window.status_code == 422

    assert (
        staff_client.post(
            "/api/loyalty/codes", json={"discount_type": "FIXED", "discount_value": "5"}
        ).status_code
        == 403
    )


def test_list_filters_search_and_paginates(
    authenticated_user, session, garage, customer, booking_request
):
    _code(session, garage, "ACTIVE1")
    _code(session, garage, "OFF1", status="DISABLED")
    _code(session, garage, "OLD1", expires_at=NOW - datetime.timedelta(days=1))
    used = _loyalty_code(session, garage, customer)
    session.add(
        DiscountRedemption(
            garage_id=garage.id,
            discount_code_id=used.id,
            booking_request_id=booking_request.id,
            amount=Decimal("1.00"),
            status="CONSUMED",
            single_use=True,
        )
    )
    session.commit()
    client = authenticated_user.client

    def codes(**params):
        body = client.get("/api/loyalty/codes", query_string=params).get_json()
        return body["total"], {i["code"] for i in body["items"]}

    assert codes()[0] == 4
    assert codes(status="ACTIVE")[1] == {"ACTIVE1"}
    assert codes(status="DISABLED")[1] == {"OFF1"}
    assert codes(status="EXPIRED")[1] == {"OLD1"}
    assert codes(status="USED")[1] == {"LOYAL-ABCDEFGH"}
    assert codes(source="LOYALTY")[1] == {"LOYAL-ABCDEFGH"}
    assert codes(search="jane")[1] == {"LOYAL-ABCDEFGH"}
    assert codes(search="loyal-abc")[1] == {"LOYAL-ABCDEFGH"}
    total, page = codes(limit=2, offset=2)
    assert total == 4 and len(page) == 2

    item = client.get("/api/loyalty/codes", query_string={"source": "LOYALTY"}).get_json()["items"][
        0
    ]
    assert item["customer"]["email"] == "jane.doe@example.com"
    assert item["uses"] == 1


def test_edit_disable_and_enable(authenticated_user, session, garage, customer, staff_client):
    promo = _code(session, garage)
    loyalty = _loyalty_code(session, garage, customer)
    client = authenticated_user.client

    body = client.patch(f"/api/loyalty/codes/{promo.id}", json={"status": "DISABLED"}).get_json()
    assert body["status"] == "DISABLED"
    body = client.patch(
        f"/api/loyalty/codes/{promo.id}", json={"status": "ACTIVE", "max_uses": 3}
    ).get_json()
    assert body["status"] == "ACTIVE" and body["max_uses"] == 3

    assert client.patch(f"/api/loyalty/codes/{loyalty.id}", json={"max_uses": 5}).status_code == 422
    assert (
        staff_client.patch(
            f"/api/loyalty/codes/{promo.id}", json={"status": "DISABLED"}
        ).status_code
        == 403
    )


def test_resend_bypasses_the_already_sent_guard(authenticated_user, session, garage, customer):
    loyalty = _loyalty_code(session, garage, customer)
    promo = _code(session, garage)
    client = authenticated_user.client

    assert client.post(f"/api/loyalty/codes/{loyalty.id}/resend").status_code == 200
    assert client.post(f"/api/loyalty/codes/{loyalty.id}/resend").status_code == 200
    assert CommunicationLog.query.filter_by(discount_code_id=loyalty.id).count() == 2
    assert client.post(f"/api/loyalty/codes/{promo.id}/resend").status_code == 422


def test_management_is_tenant_isolated(
    authenticated_user, second_authenticated_client, session, garage, customer
):
    code = _code(session, garage)
    assert second_authenticated_client.get("/api/loyalty/codes").get_json()["total"] == 0
    assert (
        second_authenticated_client.patch(
            f"/api/loyalty/codes/{code.id}", json={"status": "DISABLED"}
        ).status_code
        == 404
    )
    assert (
        second_authenticated_client.get(f"/api/loyalty/customers/{customer.id}").status_code == 404
    )


def test_customer_summary(authenticated_user, session, garage, customer, priced_type):
    _enable(session, garage, visits=3)
    for i in range(4):
        _appointment(
            session,
            garage,
            authenticated_user.user,
            customer,
            priced_type,
            status="COMPLETED",
            days=-10 + i,
        )
    _loyalty_code(session, garage, customer)
    body = authenticated_user.client.get(f"/api/loyalty/customers/{customer.id}").get_json()
    assert body["completed_visits"] == 4
    assert body["visits_to_next_reward"] == 2
    assert [c["code"] for c in body["codes"]] == ["LOYAL-ABCDEFGH"]


# --------------------------------------------------------------------------
# Unsubscribe / opt-out
# --------------------------------------------------------------------------


def test_unsubscribe_flow(client, session, garage, customer):
    token = make_token(customer)
    preview = client.post("/api/public/marketing/unsubscribe/preview", json={"token": token})
    assert preview.status_code == 200
    assert preview.get_json() == {"business_name": garage.name, "unsubscribed": False}

    done = client.post("/api/public/marketing/unsubscribe", json={"token": token})
    assert done.status_code == 200
    assert Customer.query.get(customer.id).marketing_email_opt_out is True

    tampered = client.post("/api/public/marketing/unsubscribe", json={"token": token[:-2] + "xx"})
    assert tampered.status_code == 404


def test_staff_can_set_the_opt_out(authenticated_user, customer):
    resp = authenticated_user.client.patch(
        f"/api/customers/{customer.id}", json={"marketing_email_opt_out": True}
    )
    assert resp.status_code == 200
    assert resp.get_json()["marketing_email_opt_out"] is True
    assert Customer.query.get(customer.id).marketing_email_opt_out_at is not None
