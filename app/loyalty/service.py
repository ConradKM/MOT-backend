"""Loyalty domain service - the one place that decides whether a visit
qualifies, records an earn, generates rewards, and redeems them.

Integration seam
-----------------
This module is deliberately reached from exactly one place today:
``_handle_appointment_completed`` in app/loyalty/handlers.py, registered on
the shared ``APPOINTMENT_COMPLETED`` event (see app/communications/events.py).

That single hook already covers BOTH normal appointments and walk-in/queue
visits: app/queueing/service.py::complete_service promotes a queue entry to a
real, customer-attached ``Appointment`` (created back in ``start_service``,
which resolves/creates the ``Customer`` by phone) and itself emits
``APPOINTMENT_COMPLETED`` when that appointment's status becomes COMPLETED.
So no separate queue-specific loyalty code exists or is needed - queueing
funnels into the same appointment lifecycle this service already listens to.

If a future qualifying event exists that does *not* go through Appointment
(e.g. a queue visit that should count without ever becoming a full
appointment), the seam is: call ``record_qualifying_visit(garage, customer,
source_type, source_id, price_minor=..., appointment_type_id=...)`` directly -
every earning path funnels through that one idempotent function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.loyalty.ledger import (
    ENTRY_TYPE_ADJUSTMENT,
    ENTRY_TYPE_EARN,
    ENTRY_TYPE_REDEEM,
    PROGRESS_ENTRY_TYPES,
    SOURCE_TYPE_APPOINTMENT,
    SOURCE_TYPE_MANUAL,
    SOURCE_TYPE_REWARD,
    LoyaltyLedgerEntry,
)
from app.models.loyalty.program import PROGRAM_TYPES, REWARD_TYPES, LoyaltyProgram
from app.models.loyalty.reward import (
    REWARD_STATUS_AVAILABLE,
    REWARD_STATUS_REDEEMED,
    LoyaltyReward,
)


class LoyaltyError(Exception):
    """Raised for a caller mistake (wrong tenant/customer, invalid config,
    reward not available, ...). Routes translate this to a 4xx response -
    kept provider/framework agnostic so this module has no Flask import."""

    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.message = message
        self.code = code


def get_program(garage_id: uuid.UUID) -> LoyaltyProgram | None:
    program: LoyaltyProgram | None = LoyaltyProgram.query.filter_by(garage_id=garage_id).first()
    return program


def get_or_create_program(garage_id: uuid.UUID) -> LoyaltyProgram:
    """Owner-config endpoints always have a row to read/PATCH, even before a
    garage has ever touched loyalty settings - the row starts disabled."""
    program = get_program(garage_id)
    if program is not None:
        return program
    program = LoyaltyProgram(garage_id=garage_id, enabled=False)
    db.session.add(program)
    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent first-touch race - someone else just created it.
        db.session.rollback()
        program = get_program(garage_id)
        assert program is not None
    return program


def update_program(program: LoyaltyProgram, data: dict) -> LoyaltyProgram:
    if "program_type" in data and data["program_type"] not in PROGRAM_TYPES:
        raise LoyaltyError(f"Unsupported programme type: {data['program_type']!r}")
    if "reward_type" in data and data["reward_type"] not in REWARD_TYPES:
        raise LoyaltyError(f"Unsupported reward type: {data['reward_type']!r}")
    if data.get("threshold") is not None and data["threshold"] < 1:
        raise LoyaltyError("threshold must be at least 1.")
    if data.get("earn_per_visit") is not None and data["earn_per_visit"] < 1:
        raise LoyaltyError("earn_per_visit must be at least 1.")
    if data.get("reward_value_minor") is not None and data["reward_value_minor"] < 0:
        raise LoyaltyError("reward_value_minor cannot be negative.")

    if "qualifying_appointment_type_ids" in data:
        ids = data["qualifying_appointment_type_ids"]
        program.qualifying_appointment_type_ids = [str(v) for v in ids] if ids else None

    for field in (
        "enabled",
        "name",
        "description",
        "program_type",
        "earn_per_visit",
        "threshold",
        "reward_type",
        "reward_value_minor",
        "currency",
        "min_spend_minor",
    ):
        if field in data:
            setattr(program, field, data[field])

    db.session.commit()
    return program


def _qualifies(
    program: LoyaltyProgram, *, appointment_type_id, price_at_booking: Decimal | None
) -> bool:
    if not program.enabled:
        return False
    qualifying_ids = program.qualifying_type_ids()
    if qualifying_ids is not None and str(appointment_type_id) not in qualifying_ids:
        return False
    if program.min_spend_minor:
        if price_at_booking is None:
            return False
        price_minor = int((price_at_booking * 100).to_integral_value())
        if price_minor < program.min_spend_minor:
            return False
    return True


def record_appointment_completed(appointment) -> None:
    """Handler for APPOINTMENT_COMPLETED. Idempotent: retried/replayed
    completion of the same appointment earns nothing extra, enforced by the
    DB unique constraint on (program, source_type, source_id, entry_type),
    not just by an application-side check (races/retries fail safely)."""
    program = get_program(appointment.garage_id)
    if program is None or not program.enabled:
        return
    if not _qualifies(
        program,
        appointment_type_id=appointment.appointment_type_id,
        price_at_booking=appointment.price_at_booking,
    ):
        return

    entry = LoyaltyLedgerEntry(
        garage_id=appointment.garage_id,
        program_id=program.id,
        customer_id=appointment.customer_id,
        entry_type=ENTRY_TYPE_EARN,
        units=program.earn_per_visit,
        source_type=SOURCE_TYPE_APPOINTMENT,
        source_id=str(appointment.id),
    )
    db.session.add(entry)
    try:
        db.session.commit()
    except IntegrityError:
        # Already earned for this appointment - a retry/replay, not an error.
        db.session.rollback()
        return

    _generate_pending_rewards(program, appointment.customer_id)


def lifetime_units(program_id: uuid.UUID, customer_id: uuid.UUID) -> int:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(LoyaltyLedgerEntry.units), 0))
        .filter(
            LoyaltyLedgerEntry.program_id == program_id,
            LoyaltyLedgerEntry.customer_id == customer_id,
            LoyaltyLedgerEntry.entry_type.in_(PROGRESS_ENTRY_TYPES),
        )
        .scalar()
    )
    return int(total or 0)


def _generate_pending_rewards(program: LoyaltyProgram, customer_id: uuid.UUID) -> None:
    """Create any reward cycles the customer has newly unlocked. Safe under
    concurrency: each cycle's row is protected by
    uq_loyalty_rewards_program_customer_cycle, so a duplicate insert (a race
    between two completions) fails the unique constraint and is dropped
    rather than doubling the reward."""
    total = lifetime_units(program.id, customer_id)
    target_cycles = total // program.threshold
    if target_cycles < 1:
        return
    existing_cycles = {
        row[0]
        for row in db.session.query(LoyaltyReward.cycle_number).filter(
            LoyaltyReward.program_id == program.id,
            LoyaltyReward.customer_id == customer_id,
        )
    }
    for cycle in range(1, target_cycles + 1):
        if cycle in existing_cycles:
            continue
        reward = LoyaltyReward(
            garage_id=program.garage_id,
            program_id=program.id,
            customer_id=customer_id,
            cycle_number=cycle,
            reward_type=program.reward_type,
            reward_value_minor=program.reward_value_minor,
            currency=program.currency,
        )
        db.session.add(reward)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()


@dataclass
class LoyaltyProgress:
    program: LoyaltyProgram
    lifetime_units: int
    current_units: int
    target: int
    remaining: int
    available_rewards: list[LoyaltyReward]


def get_customer_progress(garage_id: uuid.UUID, customer_id: uuid.UUID) -> LoyaltyProgress | None:
    program = get_program(garage_id)
    if program is None:
        return None
    total = lifetime_units(program.id, customer_id)
    rewards_ever_generated = LoyaltyReward.query.filter_by(
        program_id=program.id, customer_id=customer_id
    ).count()
    current = total - rewards_ever_generated * program.threshold
    available = (
        LoyaltyReward.query.filter_by(
            program_id=program.id, customer_id=customer_id, status=REWARD_STATUS_AVAILABLE
        )
        .order_by(LoyaltyReward.cycle_number)
        .all()
    )
    return LoyaltyProgress(
        program=program,
        lifetime_units=total,
        current_units=current,
        target=program.threshold,
        remaining=max(program.threshold - current, 0),
        available_rewards=available,
    )


def get_customer_history(garage_id: uuid.UUID, customer_id: uuid.UUID, limit: int = 50):
    return (
        LoyaltyLedgerEntry.query.filter_by(garage_id=garage_id, customer_id=customer_id)
        .order_by(LoyaltyLedgerEntry.created_at.desc())
        .limit(limit)
        .all()
    )


def adjust(
    *,
    garage: Garage,
    customer: Customer,
    actor: Employee,
    delta: int,
    reason: str,
) -> LoyaltyLedgerEntry:
    if customer.garage_id != garage.id:
        raise LoyaltyError("Customer does not belong to this business.", code=403)
    if delta == 0:
        raise LoyaltyError("delta must be non-zero.")
    if not reason or not reason.strip():
        raise LoyaltyError("A reason is required for a manual adjustment.")

    program = get_program(garage.id)
    if program is None or not program.enabled:
        raise LoyaltyError("Loyalty is not enabled for this business.")

    total = lifetime_units(program.id, customer.id)
    if total + delta < 0:
        raise LoyaltyError("Adjustment would make the customer's balance negative.")

    entry = LoyaltyLedgerEntry(
        garage_id=garage.id,
        program_id=program.id,
        customer_id=customer.id,
        entry_type=ENTRY_TYPE_ADJUSTMENT,
        units=delta,
        source_type=SOURCE_TYPE_MANUAL,
        source_id=None,
        actor_employee_id=actor.id,
        reason=reason.strip(),
    )
    db.session.add(entry)
    db.session.commit()

    if delta > 0:
        _generate_pending_rewards(program, customer.id)
    return entry


def apply_discount(price_minor: int, reward: LoyaltyReward) -> int:
    """Authoritative discount application - never done on the frontend. Floors
    at 0 rather than going negative; callers route a resulting 0 through the
    existing "no payment needed now" path (see app/payments/service.py /
    app/public_booking/routes.py) rather than attempting a Stripe charge."""
    return max(price_minor - reward.reward_value_minor, 0)


def redeem_reward(
    *, garage: Garage, customer: Customer, reward_id: uuid.UUID, actor: Employee
) -> LoyaltyReward:
    reward: LoyaltyReward | None = LoyaltyReward.query.filter_by(id=reward_id).first()
    if reward is None or reward.garage_id != garage.id:
        raise LoyaltyError("Reward not found.", code=404)
    if reward.customer_id != customer.id:
        raise LoyaltyError("This reward does not belong to this customer.", code=403)

    # Atomic, race-safe transition: only one concurrent redeem attempt can
    # match the WHERE status='AVAILABLE' clause and update the row.
    updated = cast(
        CursorResult,
        db.session.execute(
            update(LoyaltyReward)
            .where(LoyaltyReward.id == reward.id, LoyaltyReward.status == REWARD_STATUS_AVAILABLE)
            .values(
                status=REWARD_STATUS_REDEEMED,
                redeemed_at=datetime.now(UTC),
                redeemed_by_employee_id=actor.id,
            )
        ),
    )
    if updated.rowcount == 0:
        db.session.rollback()
        raise LoyaltyError("Reward is not available to redeem.", code=409)

    ledger_entry = LoyaltyLedgerEntry(
        garage_id=garage.id,
        program_id=reward.program_id,
        customer_id=customer.id,
        entry_type=ENTRY_TYPE_REDEEM,
        # units is excluded from progress (PROGRESS_ENTRY_TYPES) so this value
        # never affects a balance - 0 keeps the audit row honest rather than
        # implying units were "spent" from the visit count.
        units=0,
        source_type=SOURCE_TYPE_REWARD,
        source_id=str(reward.id),
        actor_employee_id=actor.id,
    )
    db.session.add(ledger_entry)
    db.session.flush()
    reward.redemption_ledger_entry_id = ledger_entry.id
    db.session.commit()
    db.session.refresh(reward)
    return reward
