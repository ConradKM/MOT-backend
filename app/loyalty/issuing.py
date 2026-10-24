"""Issuing loyalty reward codes.

A *visit* is a COMPLETED appointment for that customer at that business.
Every ``visits_required``-th visit issues one personal, single-use code, and
the count keeps going toward the next one.

Which visit a completion is
---------------------------
A customer's completed appointments are ordered by ``completed_at`` (nulls -
history that predates the column, and imported history - first), then start
time. A completion's *position* in that order is the visit number it
represents; the code it may issue is for that number. Using the position of
*this* appointment rather than "the count right now" is what makes two
completions racing for the same customer each land on their own visit
number. The customer row is locked while the position is computed, and the
unique ``(customer_id, issued_for_visit_count)`` constraint makes issuing
exactly-once whatever happens (a retried request, a completed -> reopened ->
completed flap).

What never issues
-----------------
History counts toward the total, but only a *new* completion in the app can
trigger a code: switching the programme on never mass-issues or mass-emails,
and bulk-imported appointments (app/bulk_import/) never set ``completed_at``,
so they can count but can't trigger.

Why not an event handler
------------------------
app/communications/events.py::emit_event swallows handler failures by
design, which would silently skip a customer's reward. Routes call
:func:`issue_reward_safely` explicitly after the completion is committed;
it can never block the completion itself, logs any failure, and
``flask loyalty-reconcile`` (app/loyalty/cli.py) catches up anything missed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from flask import current_app
from sqlalchemy import nulls_first
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.customer import Customer
from app.models.loyalty.discount_code import (
    DISCOUNT_SOURCE_LOYALTY,
    DISCOUNT_STATUS_ACTIVE,
    DiscountCode,
)
from app.models.loyalty.loyalty_settings import LoyaltySettings

from .codes import LOYALTY_PREFIX, generate_code, normalize_code
from .service import get_loyalty_settings

_CODE_ATTEMPTS = 5


def completed_visits(customer_id: uuid.UUID, garage_id: uuid.UUID) -> list[Appointment]:
    return cast(
        list[Appointment],
        Appointment.query.filter_by(
            customer_id=customer_id, garage_id=garage_id, status="COMPLETED"
        )
        .order_by(
            nulls_first(Appointment.completed_at.asc()),
            Appointment.start_time.asc(),
            Appointment.id.asc(),
        )
        .all(),
    )


def _code_exists(customer_id: uuid.UUID, visit_count: int) -> bool:
    return (
        db.session.query(DiscountCode.id)
        .filter_by(customer_id=customer_id, issued_for_visit_count=visit_count)
        .first()
        is not None
    )


def create_unique_code(build, *, garage_id: uuid.UUID, prefix: str = "") -> DiscountCode | None:
    """Insert ``build(code_text)`` with a freshly generated code, retrying on
    a code collision. Returns None if the row couldn't be inserted for any
    other uniqueness reason (e.g. the milestone was issued concurrently)."""
    for _ in range(_CODE_ATTEMPTS):
        text = generate_code(prefix)
        normalised = normalize_code(text)
        if (
            DiscountCode.query.filter_by(garage_id=garage_id, code_normalized=normalised).first()
            is not None
        ):
            continue
        row = build(text, normalised)
        try:
            with db.session.begin_nested():
                db.session.add(row)
        except IntegrityError:
            if (
                DiscountCode.query.filter_by(
                    garage_id=garage_id, code_normalized=normalised
                ).first()
                is None
            ):
                return None
            continue
        return cast(DiscountCode, row)
    raise RuntimeError("Could not generate a unique discount code.")


def _issue(
    settings: LoyaltySettings,
    customer: Customer,
    visit_count: int,
    appointment: Appointment,
    now: datetime,
) -> DiscountCode | None:
    if _code_exists(customer.id, visit_count):
        return None

    def build(text: str, normalised: str) -> DiscountCode:
        return DiscountCode(
            garage_id=settings.garage_id,
            code=text,
            code_normalized=normalised,
            source=DISCOUNT_SOURCE_LOYALTY,
            discount_type=settings.discount_type,
            discount_value=settings.discount_value,
            status=DISCOUNT_STATUS_ACTIVE,
            customer_id=customer.id,
            valid_from=now,
            expires_at=now + timedelta(days=settings.expiry_days),
            max_uses=1,
            max_uses_per_customer=1,
            issued_for_visit_count=visit_count,
            issued_for_appointment_id=appointment.id,
        )

    return create_unique_code(build, garage_id=settings.garage_id, prefix=LOYALTY_PREFIX)


def issue_reward_for_completion(
    appointment: Appointment, now: datetime | None = None
) -> DiscountCode | None:
    """Issue (and email) the reward ``appointment``'s completion earned, if
    any. Commits. Returns the new code, or None when nothing was due."""
    now = now or datetime.now(UTC)
    if appointment.status != "COMPLETED" or appointment.completed_at is None:
        return None
    settings = get_loyalty_settings(appointment.garage_id)
    if settings is None or not settings.enabled:
        return None

    # Serialise issuing per customer (see the module docstring).
    customer = (
        Customer.query.filter_by(id=appointment.customer_id, garage_id=appointment.garage_id)
        .with_for_update()
        .first()
    )
    if customer is None:
        db.session.rollback()
        return None

    visits = completed_visits(customer.id, appointment.garage_id)
    position = next((i + 1 for i, a in enumerate(visits) if a.id == appointment.id), None)
    if position is None or position % settings.visits_required != 0:
        db.session.commit()
        return None

    code = _issue(settings, customer, position, appointment, now)
    db.session.commit()
    if code is not None:
        current_app.logger.info(
            "LOYALTY_CODE_ISSUED garage=%s customer=%s visit=%s",
            appointment.garage_id,
            customer.id,
            position,
        )
        _send_reward_email(code)
    return code


def issue_reward_safely(appointment: Appointment) -> None:
    """The guarded call routes make after committing a completion - a
    loyalty bug must never fail (or roll back) completing an appointment."""
    try:
        issue_reward_for_completion(appointment)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "LOYALTY_ISSUE_FAILED garage=%s appointment=%s - run `flask loyalty-reconcile`",
            appointment.garage_id,
            appointment.id,
        )


def _send_reward_email(code: DiscountCode) -> None:
    from app.email.service import send_loyalty_reward_email

    try:
        send_loyalty_reward_email(code)
    except Exception:  # never let an email problem undo an issued code
        db.session.rollback()
        current_app.logger.exception("LOYALTY_EMAIL_FAILED code_id=%s", code.id)


def reconcile(*, days: int = 30, now: datetime | None = None) -> list[DiscountCode]:
    """Issue any reward a completion in the last ``days`` earned but didn't
    get (a crash between completing and issuing, say). Only completions
    made in the app after the programme was switched on are considered, so
    this can never mass-issue history. Safe to run any number of times."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    issued: list[DiscountCode] = []
    for settings in LoyaltySettings.query.filter_by(enabled=True).all():
        window_start = max(since, settings.enabled_at or since)
        customer_ids = [
            row[0]
            for row in db.session.query(Appointment.customer_id)
            .filter(
                Appointment.garage_id == settings.garage_id,
                Appointment.status == "COMPLETED",
                Appointment.completed_at >= window_start,
            )
            .distinct()
            .all()
        ]
        for customer_id in customer_ids:
            customer = Customer.query.filter_by(id=customer_id).with_for_update().first()
            if customer is None:
                continue
            visits = completed_visits(customer_id, settings.garage_id)
            new_codes = []
            for position, appointment in enumerate(visits, start=1):
                if (
                    position % settings.visits_required == 0
                    and appointment.completed_at is not None
                    and appointment.completed_at >= window_start
                ):
                    code = _issue(settings, customer, position, appointment, now)
                    if code is not None:
                        new_codes.append(code)
            db.session.commit()
            for code in new_codes:
                _send_reward_email(code)
            issued.extend(new_codes)
    return issued


def loyalty_summary(customer: Customer) -> dict:
    settings = get_loyalty_settings(customer.garage_id)
    visits = len(completed_visits(customer.id, customer.garage_id))
    enabled = settings is not None and settings.enabled
    to_next = None
    if enabled and settings is not None:
        to_next = settings.visits_required - (visits % settings.visits_required)
    return {
        "enabled": enabled,
        "visits_required": settings.visits_required if settings is not None else None,
        "completed_visits": visits,
        "visits_to_next_reward": to_next,
    }
