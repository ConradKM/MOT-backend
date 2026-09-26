"""Loyalty domain tests.

Covers programme configuration, tenant isolation, idempotent earning on
appointment completion (the same event walk-in/queue completion emits - see
app/loyalty/service.py module docstring), threshold/reward generation,
redemption safety, and manual adjustments.
"""

import datetime
import threading

import pytest
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.loyalty import service
from app.loyalty.handlers import register_loyalty_handlers
from app.models.appointments.appointment import Appointment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.loyalty.ledger import ENTRY_TYPE_ADJUSTMENT, ENTRY_TYPE_EARN, LoyaltyLedgerEntry
from app.models.loyalty.program import LoyaltyProgram
from app.models.loyalty.reward import REWARD_STATUS_AVAILABLE, REWARD_STATUS_REDEEMED, LoyaltyReward
from app.models.queueing.queue_settings import GarageQueueSettings
from app.queueing import service as queueing_service

_MONDAY = datetime.date(2026, 3, 2)


def _at(hour, minute=0, day=_MONDAY):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=datetime.UTC)


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
    resp = authenticated_client.patch(
        f"/api/appointments/{appt.id}", json={"status": "IN_PROGRESS"}
    )
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


def test_disabled_program_does_not_earn(
    authenticated_client, make_appointment, customer, session, garage
):
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
    return LoyaltyReward.query.filter_by(
        customer_id=customer.id, status=REWARD_STATUS_AVAILABLE
    ).one()


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


def test_duplicate_redemption_rejected(
    authenticated_client, enabled_program, make_appointment, customer
):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    first = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem"
    )
    second = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem"
    )
    assert first.status_code == 200
    assert second.status_code == 409


def test_redeeming_unavailable_reward_rejected(
    session, garage, customer, authenticated_client, enabled_program
):
    reward = LoyaltyReward(
        garage_id=garage.id,
        program_id=enabled_program.id,
        customer_id=customer.id,
        cycle_number=1,
        status=REWARD_STATUS_REDEEMED,
        threshold_at_generation=enabled_program.threshold,
        reward_type=enabled_program.reward_type,
        reward_value_minor=enabled_program.reward_value_minor,
        currency=enabled_program.currency,
    )
    session.add(reward)
    session.commit()

    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/rewards/{reward.id}/redeem"
    )
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


def test_negative_adjustment_cannot_make_balance_negative(
    authenticated_client, enabled_program, customer
):
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


# --------------------------------------------------------------------------
# Historical integrity of a programme edit (threshold/reward changes must
# never retroactively corrupt already-unlocked cycles)
# --------------------------------------------------------------------------


def test_threshold_increase_after_a_reward_leaves_progress_correct(
    authenticated_client, enabled_program, make_appointment, customer
):
    """threshold 5 -> earn a reward at 5 -> owner raises threshold to 10.
    Progress toward the *next* reward must be computed against the new
    threshold using only unconsumed units, never by multiplying the old
    reward's count against the new threshold (which produced a negative
    current_units before this was fixed)."""
    now = datetime.datetime.now(datetime.UTC)
    for i in range(5):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 0
    assert progress["reward_available"] is True

    resp = authenticated_client.patch("/api/loyalty/program", json={"threshold": 10})
    assert resp.status_code == 200

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 0  # not -5
    assert progress["target"] == 10
    assert progress["remaining"] == 10
    # The already-earned reward is untouched.
    assert progress["reward_available"] is True
    assert len(progress["available_rewards"]) == 1

    # One more visit now counts toward the *new* 10-visit cycle.
    appt = make_appointment(now + datetime.timedelta(days=5), status="BOOKED")
    _complete(authenticated_client, appt)
    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 1
    assert progress["remaining"] == 9


def test_threshold_decrease_immediately_unlocks_accrued_progress(
    authenticated_client, enabled_program, make_appointment, customer
):
    """threshold 5, customer at 3/5 (no reward yet) -> owner lowers threshold
    to 4. The 4th qualifying visit now unlocks a reward under the new,
    easier threshold - and the reward's own snapshot should reflect the
    threshold that generated it (4), not the original (5)."""
    now = datetime.datetime.now(datetime.UTC)
    for i in range(3):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 3
    assert progress["reward_available"] is False

    authenticated_client.patch("/api/loyalty/program", json={"threshold": 4})

    # Reward generation only runs on a new earn/adjustment, not on the
    # PATCH itself - trigger one more qualifying visit.
    appt = make_appointment(now + datetime.timedelta(days=3), status="BOOKED")
    _complete(authenticated_client, appt)

    reward = LoyaltyReward.query.filter_by(customer_id=customer.id).one()
    assert reward.threshold_at_generation == 4

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["reward_available"] is True
    assert progress["current_units"] == 0  # 4 lifetime - 4 consumed by the new reward
    assert progress["target"] == 4


def test_reward_value_change_does_not_mutate_already_generated_reward(
    authenticated_client, enabled_program, make_appointment, customer
):
    reward = _earn_one_reward(authenticated_client, enabled_program, make_appointment, customer)
    assert reward.reward_value_minor == 1000

    authenticated_client.patch("/api/loyalty/program", json={"reward_value_minor": 500})

    db.session.refresh(reward)
    assert reward.reward_value_minor == 1000  # unchanged - snapshotted at generation

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["available_rewards"][0]["reward_value_minor"] == 1000
    # New config only affects rewards generated from here on.
    assert progress["reward_value_minor"] == 500


def test_disabling_and_re_enabling_preserves_progress(
    authenticated_client, enabled_program, make_appointment, customer
):
    now = datetime.datetime.now(datetime.UTC)
    for i in range(3):
        appt = make_appointment(now + datetime.timedelta(days=i), status="BOOKED")
        _complete(authenticated_client, appt)

    authenticated_client.patch("/api/loyalty/program", json={"enabled": False})
    authenticated_client.patch("/api/loyalty/program", json={"enabled": True})

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == 3


# --------------------------------------------------------------------------
# Threshold / bulk-adjustment boundary cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lifetime", "expected_rewards", "expected_current"),
    [
        (0, 0, 0),
        (1, 0, 1),
        (4, 0, 4),
        (5, 1, 0),
        (6, 1, 1),
        (9, 1, 4),
        (10, 2, 0),
        (11, 2, 1),
    ],
)
def test_bulk_adjustment_boundaries(
    authenticated_client,
    enabled_program,
    customer,
    user,
    lifetime,
    expected_rewards,
    expected_current,
):
    """A single bulk adjustment from 0 must generate exactly the number of
    reward cycles the lifetime total actually crosses - no duplicates, none
    missing, deterministic regardless of how many thresholds it jumps."""
    if lifetime:
        service.adjust(
            garage=user.garage, customer=customer, actor=user, delta=lifetime, reason="bulk"
        )

    rewards = LoyaltyReward.query.filter_by(customer_id=customer.id).all()
    assert len(rewards) == expected_rewards
    assert {r.cycle_number for r in rewards} == set(range(1, expected_rewards + 1))

    progress = authenticated_client.get(f"/api/loyalty/customers/{customer.id}/progress").json
    assert progress["current_units"] == expected_current
    assert progress["lifetime_units"] == lifetime


# --------------------------------------------------------------------------
# Genuine concurrency (real threads, real separate DB sessions/transactions -
# not sleeps). Each worker thread pushes its own Flask app context, which
# Flask-SQLAlchemy scopes db.session by, so each thread gets its own
# connection/transaction exactly like two simultaneous requests would.
# --------------------------------------------------------------------------


def test_concurrent_duplicate_completion_events_earn_exactly_once(
    app, enabled_program, make_appointment, customer
):
    appt = make_appointment(datetime.datetime.now(datetime.UTC), status="BOOKED")
    appt.status = "COMPLETED"
    db.session.commit()
    appointment_id = appt.id

    errors: list[Exception] = []

    def attempt() -> None:
        with app.app_context():
            try:
                a = Appointment.query.filter_by(id=appointment_id).one()
                service.record_appointment_completed(a)
            except Exception as exc:  # noqa: BLE001 - a thread's failure must not vanish silently
                errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    entries = LoyaltyLedgerEntry.query.filter_by(
        customer_id=customer.id, entry_type=ENTRY_TYPE_EARN
    ).all()
    assert len(entries) == 1


def test_concurrent_negative_adjustments_cannot_drive_balance_negative(
    app, garage, customer, user, enabled_program
):
    """Customer has exactly 1 unit. Two staff simultaneously try to remove
    it. Without app/loyalty/service.py::adjust's row lock on the customer,
    both requests can read the same stale total, both pass the "can't go
    negative" check, and the balance ends up at -1. Exactly one must
    succeed; the other must be rejected; the balance must never go negative."""
    service.adjust(garage=garage, customer=customer, actor=user, delta=1, reason="bootstrap")

    garage_id, customer_id, actor_id, program_id = (
        garage.id,
        customer.id,
        user.id,
        enabled_program.id,
    )
    results: list[str] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        with app.app_context():
            g = Garage.query.filter_by(id=garage_id).one()
            c = Customer.query.filter_by(id=customer_id).one()
            e = Employee.query.filter_by(id=actor_id).one()
            try:
                service.adjust(garage=g, customer=c, actor=e, delta=-1, reason="concurrent removal")
                outcome = "ok"
            except service.LoyaltyError:
                outcome = "rejected"
            with results_lock:
                results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(results) == ["ok", "rejected"]
    assert service.lifetime_units(program_id, customer_id) == 0


# --------------------------------------------------------------------------
# Queue / walk-in earning, verified end-to-end through the real queue API
# (not merely trusted from the appointment-completion tests above)
# --------------------------------------------------------------------------


def test_queue_walkin_completion_earns_exactly_one_visit(
    client,
    authenticated_client,
    enabled_program,
    garage,
    garage_schedule,
    appointment_type,
    session,
    monkeypatch,
):
    # Freeze the queue's clock to a weekday within the default Mon-Fri
    # 09:00-17:00 schedule (see tests/api/test_walkin_queue.py) - otherwise
    # this test is flaky depending on what day/time it happens to run.
    monkeypatch.setattr(queueing_service, "utcnow", lambda: _at(10))
    session.add(GarageQueueSettings(garage_id=garage.id, is_open=True, no_show_timeout_minutes=10))
    session.commit()

    join_resp = client.post(
        "/api/public/garage-a/queue/join",
        json={
            "customer_first_name": "Walkin",
            "customer_last_name": "Customer",
            "customer_phone": "07123 456999",
            "sms_opt_in": False,
        },
    )
    assert join_resp.status_code == 201

    # The public join response is customer-facing (token only, no internal
    # id) - the staff call-next response is what carries the entry's id.
    called = authenticated_client.post("/api/queue/call-next")
    assert called.status_code == 200
    entry_id = called.get_json()["id"]

    started = authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert started.status_code == 200
    walkin_customer_id = (
        Appointment.query.filter_by(id=started.get_json()["appointment_id"]).one().customer_id
    )

    done = authenticated_client.post(f"/api/queue/entries/{entry_id}/complete")
    assert done.status_code == 200

    progress = authenticated_client.get(
        f"/api/loyalty/customers/{walkin_customer_id}/progress"
    ).json
    assert progress["current_units"] == 1
    assert progress["lifetime_units"] == 1

    # A replayed/duplicate completion request is rejected by the queue route
    # itself (already DONE) before loyalty is ever touched again.
    retry = authenticated_client.post(f"/api/queue/entries/{entry_id}/complete")
    assert retry.status_code == 409
    progress_after = authenticated_client.get(
        f"/api/loyalty/customers/{walkin_customer_id}/progress"
    ).json
    assert progress_after["current_units"] == 1


def test_queue_entry_cancelled_before_service_earns_nothing(
    client, garage, garage_schedule, appointment_type, session, monkeypatch
):
    monkeypatch.setattr(queueing_service, "utcnow", lambda: _at(10))
    session.add(GarageQueueSettings(garage_id=garage.id, is_open=True, no_show_timeout_minutes=10))
    session.commit()

    join_resp = client.post(
        "/api/public/garage-a/queue/join",
        json={
            "customer_first_name": "Leaves",
            "customer_last_name": "Early",
            "customer_phone": "07123 456998",
            "sms_opt_in": False,
        },
    )
    token = join_resp.get_json()["token"]

    cancel = client.post("/api/public/garage-a/queue/cancel", json={"token": token})
    assert cancel.status_code == 200

    # No Appointment/Customer was ever created for this walk-in, so there is
    # nothing to check loyalty against - the important assertion is simply
    # that no ledger entry exists anywhere for this business.
    assert LoyaltyLedgerEntry.query.filter_by(garage_id=garage.id).count() == 0


# --------------------------------------------------------------------------
# Qualifying-service configuration must be tenant-scoped
# --------------------------------------------------------------------------


def test_qualifying_type_from_another_tenant_is_rejected(
    authenticated_client, second_garage, session
):
    from app.models.appointments.appointment_type import GarageAppointmentType

    foreign_type = GarageAppointmentType(
        garage_id=second_garage.id,
        name="Someone else's service",
        base_price=20,
        default_duration_minutes=30,
        status="ACTIVE",
    )
    session.add(foreign_type)
    session.commit()

    resp = authenticated_client.patch(
        "/api/loyalty/program",
        json={"qualifying_appointment_type_ids": [str(foreign_type.id)]},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Sane upper bounds against absurd/overflow-prone configuration
# --------------------------------------------------------------------------


def test_threshold_above_max_is_rejected(authenticated_client):
    resp = authenticated_client.patch("/api/loyalty/program", json={"threshold": 1_000_000})
    assert resp.status_code == 422


def test_reward_value_above_max_is_rejected(authenticated_client):
    resp = authenticated_client.patch(
        "/api/loyalty/program", json={"reward_value_minor": 1_000_000_00}
    )
    assert resp.status_code == 422


def test_adjust_delta_above_max_is_rejected(authenticated_client, enabled_program, customer):
    resp = authenticated_client.post(
        f"/api/loyalty/customers/{customer.id}/adjust",
        json={"delta": 1_000_000, "reason": "typo"},
    )
    assert resp.status_code == 422


def test_non_alphabetic_currency_is_rejected(authenticated_client):
    """The schema only checked len == 3 before this - "123" passed that but
    made the frontend's Intl.NumberFormat throw. A well-formed ISO-shaped
    code is required now."""
    resp = authenticated_client.patch("/api/loyalty/program", json={"currency": "123"})
    assert resp.status_code == 422


def test_currency_is_normalised_to_uppercase(authenticated_client):
    resp = authenticated_client.patch("/api/loyalty/program", json={"currency": "gbp"})
    assert resp.status_code == 200
    assert resp.json["currency"] == "GBP"
