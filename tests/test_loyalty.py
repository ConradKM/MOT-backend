"""Loyalty domain tests.

Covers programme configuration, tenant isolation, idempotent earning on
appointment completion (the same event walk-in/queue completion emits - see
app/loyalty/service.py module docstring), threshold/reward generation,
redemption safety, and manual adjustments.
"""

import datetime

import pytest
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.loyalty import service
from app.loyalty.handlers import register_loyalty_handlers
from app.models.employee import Employee
from app.models.loyalty.ledger import ENTRY_TYPE_ADJUSTMENT, ENTRY_TYPE_EARN, LoyaltyLedgerEntry
from app.models.loyalty.program import LoyaltyProgram
from app.models.loyalty.reward import REWARD_STATUS_AVAILABLE, REWARD_STATUS_REDEEMED, LoyaltyReward


@pytest.fixture(autouse=True)
def _loyalty_handlers_registered():
    """The event bus registry is process-global and reset by other tests'
    ``_reset_handlers_for_tests`` - re-register defensively so completing an
    appointment in this test module always reaches the loyalty handler
    regardless of test order."""
    register_loyalty_handlers()
    yield


@pytest.fixture()
def staff_user(session, garage, staff_role):
    """A non-owner employee in the primary garage, for owner_required checks."""
    u = Employee(
        garage_id=garage.id,
        email="staff-a@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
        roles=[staff_role],
    )
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def staff_client(client, app, staff_user):
    from flask_jwt_extended import create_access_token

    from tests.conftest import AuthenticatedClient

    token = create_access_token(identity=str(staff_user.id))
    return AuthenticatedClient(client, token)


@pytest.fixture()
def enabled_program(session, garage):
    program = LoyaltyProgram(
        garage_id=garage.id,
        enabled=True,
        name="Regular Customer Reward",
        earn_per_visit=1,
        threshold=5,
        reward_value_minor=1000,
        currency="GBP",
    )
    session.add(program)
    session.commit()
    return program


def _complete(client, appointment):
    return client.patch(f"/api/appointments/{appointment.id}", json={"status": "COMPLETED"})


# --------------------------------------------------------------------------
# Programme configuration
# --------------------------------------------------------------------------


def test_get_program_creates_disabled_row_by_default(authenticated_client):
    resp = authenticated_client.get("/api/loyalty/program")
    assert resp.status_code == 200
    assert resp.json["enabled"] is False


def test_owner_can_configure_program(authenticated_client):
    resp = authenticated_client.patch(
        "/api/loyalty/program",
        json={"enabled": True, "name": "Regulars Club", "threshold": 5, "reward_value_minor": 1000},
    )
    assert resp.status_code == 200
    assert resp.json["enabled"] is True
    assert resp.json["name"] == "Regulars Club"
    assert resp.json["threshold"] == 5


def test_staff_cannot_configure_program(staff_client):
    resp = staff_client.patch("/api/loyalty/program", json={"enabled": True})
    assert resp.status_code == 403


def test_invalid_program_type_rejected(authenticated_client):
    resp = authenticated_client.patch("/api/loyalty/program", json={"program_type": "BOGUS"})
    assert resp.status_code == 422


def test_second_garage_cannot_see_primary_garage_program(
    authenticated_client, second_authenticated_client, enabled_program
):
    mine = authenticated_client.get("/api/loyalty/program").json
    theirs = second_authenticated_client.get("/api/loyalty/program").json
    assert mine["id"] != theirs["id"]
    assert theirs["enabled"] is False


# --------------------------------------------------------------------------
# Earning on appointment completion
# --------------------------------------------------------------------------


def test_completed_qualifying_appointment_earns_one_visit(
    authenticated_client, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    resp = _complete(authenticated_client, appt)
    assert resp.status_code == 200

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 1
    assert progress["lifetime_units"] == 1
    assert progress["reward_available"] is False


def test_retrying_completion_does_not_double_earn(
    authenticated_client, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    _complete(authenticated_client, appt)
    # Directly re-invoke the idempotent earn path, simulating a replayed
    # event / retried webhook rather than a second HTTP completion (the
    # appointment is already COMPLETED so the route itself would no-op the
    # transition - the ledger's own idempotency is what this test proves).
    service.record_appointment_completed(appt)

    entries = LoyaltyLedgerEntry.query.filter_by(
        customer_id=customer.id, entry_type=ENTRY_TYPE_EARN
    ).all()
    assert len(entries) == 1


def test_non_completed_status_does_not_earn(
    authenticated_client, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    resp = authenticated_client.patch(f"/api/appointments/{appt.id}", json={"status": "IN_PROGRESS"})
    assert resp.status_code == 200

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 0


def test_cancelled_appointment_does_not_earn(
    authenticated_client, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    authenticated_client.patch(f"/api/appointments/{appt.id}", json={"status": "CANCELLED"})

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 0


def test_disabled_program_does_not_earn(authenticated_client, make_appointment, customer, session, garage):
    program = LoyaltyProgram(garage_id=garage.id, enabled=False)
    session.add(program)
    session.commit()

    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    _complete(authenticated_client, appt)

    total = service.lifetime_units(program.id, customer.id)
    assert total == 0


def test_non_qualifying_appointment_type_does_not_earn(
    session, garage, authenticated_client, make_appointment, customer, appointment_type
):
    program = LoyaltyProgram(
        garage_id=garage.id,
        enabled=True,
        qualifying_appointment_type_ids=["00000000-0000-0000-0000-000000000000"],
    )
    session.add(program)
    session.commit()

    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    _complete(authenticated_client, appt)

    assert service.lifetime_units(program.id, customer.id) == 0


# --------------------------------------------------------------------------
# Threshold / reward generation
# --------------------------------------------------------------------------


def test_reward_generated_exactly_at_threshold(
    authenticated_client, enabled_program, make_appointment, customer
):
    now = datetime.datetime.now(datetime.UTC)
    for i in range(5):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 0
    assert progress["reward_available"] is True
    assert len(progress["available_rewards"]) == 1


def test_multiple_cycles_generate_multiple_rewards(
    authenticated_client, enabled_program, make_appointment, customer
):
    now = datetime.datetime.now(datetime.UTC)
    for i in range(11):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)

    rewards = LoyaltyReward.query.filter_by(customer_id=customer.id).all()
    assert len(rewards) == 2
    assert {r.cycle_number for r in rewards} == {1, 2}
    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 1


def test_reward_generation_is_not_duplicated_by_concurrent_style_retry(
    enabled_program, make_appointment, customer, authenticated_client
):
    now = datetime.datetime.now(datetime.UTC)
    for i in range(5):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)
        service.record_appointment_completed(appt)  # simulate a replay each time

    rewards = LoyaltyReward.query.filter_by(customer_id=customer.id).all()
    assert len(rewards) == 1


# --------------------------------------------------------------------------
# Redemption
# --------------------------------------------------------------------------


def _earn_one_reward(client, program, make_appointment, customer):
    now = datetime.datetime.now(datetime.UTC)
    for i in range(program.threshold):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(client, appt)
    return LoyaltyReward.query.filter_by(customer_id=customer.id, status=REWARD_STATUS_AVAILABLE).one()


def test_valid_redemption(authenticated_client, enabled_program, make_appointment, customer):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)

    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem"
    )
    assert resp.status_code == 200
    assert resp.json["status"] == REWARD_STATUS_REDEEMED

    db.session.refresh(reward)
    assert reward.status == REWARD_STATUS_REDEEMED
    assert reward.redeemed_at is not None


def test_duplicate_redemption_rejected(authenticated_client, enabled_program, make_appointment, customer):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    first = authenticated_client.post(f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem")
    second = authenticated_client.post(f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem")
    assert first.status_code == 200
    assert second.status_code == 409


def test_redeeming_unavailable_reward_rejected(session, garage, customer, authenticated_client, enabled_program):
    reward = LoyaltyReward(
        garage_id=garage.id,
        program_id=enabled_program.id,
        customer_id=customer.id,
        cycle_number=1,
        status=REWARD_STATUS_REDEEMED,
        reward_type=enabled_program.reward_type,
        reward_value_minor=enabled_program.reward_value_minor,
        currency=enabled_program.currency,
    )
    session.add(reward)
    session.commit()

    resp = authenticated_client.post(f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem")
    assert resp.status_code == 409


def test_wrong_tenant_cannot_redeem_reward(
    authenticated_client,
    second_authenticated_client,
    enabled_program,
    make_appointment,
    customer,
):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    resp = second_authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem"
    )
    # The customer itself isn't visible cross-tenant, so this 404s before
    # the reward's own tenant check is ever reached - both routes are
    # protected either way.
    assert resp.status_code == 404


def test_wrong_customer_cannot_redeem_others_reward(
    authenticated_client, enabled_program, make_appointment, customer, session, garage
):
    from app.models.customer import Customer

    other_customer = Customer(
        garage_id=garage.id, first_name="Other", last_name="Person", phone="+44 7700 900999"
    )
    session.add(other_customer)
    session.commit()

    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{other_customer.id}/rewards/{reward.id}/redeem"
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Manual adjustments
# --------------------------------------------------------------------------


def test_owner_can_adjust_balance_up(authenticated_client, enabled_program, customer):
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust", json={"delta": 2, "reason": "Goodwill"}
    )
    assert resp.status_code == 200
    assert resp.json["entry_type"] == ENTRY_TYPE_ADJUSTMENT

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 2


def test_staff_cannot_adjust_balance(staff_client, enabled_program, customer):
    resp = staff_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust", json={"delta": 2, "reason": "Goodwill"}
    )
    assert resp.status_code == 403


def test_adjustment_requires_reason(authenticated_client, enabled_program, customer):
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust", json={"delta": 1, "reason": ""}
    )
    assert resp.status_code in (400, 422)


def test_negative_adjustment_cannot_make_balance_negative(authenticated_client, enabled_program, customer):
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust", json={"delta": -1, "reason": "Correction"}
    )
    assert resp.status_code == 422


def test_adjustment_can_trigger_reward_generation(authenticated_client, enabled_program, customer):
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust",
        json={"delta": 5, "reason": "Manual catch-up from paper card"},
    )
    assert resp.status_code == 200
    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["reward_available"] is True


# --------------------------------------------------------------------------
# History / disabling preserves ledger
# --------------------------------------------------------------------------


def test_disabling_program_preserves_ledger_and_rewards(
    authenticated_client, enabled_program, make_appointment, customer
):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    authenticated_client.patch("/api/loyalty/program", json={"enabled": False})

    history = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/history").json
    assert len(history) == 5

    db.session.refresh(reward)
    assert reward.status == REWARD_STATUS_AVAILABLE

    # Disabled programme earns nothing further.
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    _complete(authenticated_client, appt)
    assert service.lifetime_units(enabled_program.id, customer.id) == 5


def test_tenant_isolation_on_progress_and_history(
    authenticated_client, second_authenticated_client, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    _complete(authenticated_client, appt)

    resp = second_authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress")
    assert resp.status_code == 404
