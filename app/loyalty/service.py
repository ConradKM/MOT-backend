"""Discount codes: validation, redemption and the redemption lifecycle.

``evaluate_code`` is the one place that decides whether a code applies to a
booking and what it is worth. Every entry point uses it - the public
checkout's advisory validate endpoint, the public booking submit and
deposit-intent endpoints (which re-run it and are authoritative), and the
staff appointment create/update - so none of them can disagree. Clients only
ever send the code text; the price and the discount are always computed here.

Money follows app/payments/money.py: ``Decimal`` pounds, rounded to the penny
with ROUND_HALF_UP, never floats. The discount is never more than the total.

Redemption lifecycle (mirrors the capacity-hold approach of deposit bookings):

    RESERVED  - a booking request holds the code while it waits for its
                deposit (AWAITING_PAYMENT).
    CONSUMED  - the booking is confirmed: a plain booking at submission, a
                deposit booking when its payment succeeds, a staff-created
                appointment immediately.
    RELEASED  - the booking fell through (request EXPIRED/REJECTED/CANCELLED,
                or the appointment CANCELLED/NO_SHOW) and the use is given
                back; the code is usable again if it hasn't expired.

The explicit ``sync_*`` calls at each transition keep this accurate, and
``_release_stale_redemptions`` re-derives it from the bookings themselves
before any limit is checked, so a missed transition can never permanently
use up a customer's code.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from flask import current_app
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.booking_request import BookingRequest
from app.models.customer import Customer
from app.models.loyalty.discount_code import (
    DISCOUNT_SOURCE_LOYALTY,
    DISCOUNT_STATUS_DISABLED,
    DiscountCode,
)
from app.models.loyalty.discount_redemption import (
    ACTIVE_REDEMPTION_STATUSES,
    REDEMPTION_CONSUMED,
    REDEMPTION_RELEASED,
    REDEMPTION_RESERVED,
    DiscountRedemption,
)
from app.models.loyalty.loyalty_settings import (
    DISCOUNT_TYPE_FIXED,
    DISCOUNT_TYPE_PERCENTAGE,
    DISCOUNT_TYPES,
    LoyaltySettings,
)

from .codes import is_valid_code, normalize_code

PENNY = Decimal("0.01")

# Failure reasons. The public checkout collapses every reason in
# GENERIC_FAILURES into one message so a code can't be probed: "unknown",
# "expired", "used" and "someone else's" all look identical from outside.
NOT_FOUND = "not_found"
DISABLED = "disabled"
NOT_YET_VALID = "not_yet_valid"
EXPIRED = "expired"
USED_UP = "used_up"
CUSTOMER_LIMIT = "customer_limit"
WRONG_CUSTOMER = "wrong_customer"
UNPRICED = "unpriced"
NOTHING_TO_DISCOUNT = "nothing_to_discount"

GENERIC_FAILURES = frozenset(
    {NOT_FOUND, DISABLED, NOT_YET_VALID, EXPIRED, USED_UP, CUSTOMER_LIMIT, WRONG_CUSTOMER}
)
GENERIC_FAILURE_MESSAGE = "This discount code isn't valid for this booking."

_STAFF_MESSAGES = {
    NOT_FOUND: "No discount code with that name exists for this business.",
    DISABLED: "This discount code has been disabled.",
    NOT_YET_VALID: "This discount code isn't valid yet.",
    EXPIRED: "This discount code has expired.",
    USED_UP: "This discount code has already been used.",
    CUSTOMER_LIMIT: "This customer has already used this code as many times as it allows.",
    WRONG_CUSTOMER: "This loyalty code belongs to a different customer.",
    UNPRICED: "This service has no set price, so a discount code can't be applied.",
    NOTHING_TO_DISCOUNT: "This booking is already free, so there's nothing to discount.",
}

# Customer-facing for the two reasons that reveal nothing about a code.
_PUBLIC_MESSAGES = {
    UNPRICED: "This service doesn't have a set price, so a discount code can't be applied.",
    NOTHING_TO_DISCOUNT: "This booking is already free, so there's nothing to discount.",
}

_TERMINAL_REQUEST_STATUSES = ("REJECTED", "EXPIRED", "CANCELLED")
_RELEASING_APPOINTMENT_STATUSES = ("CANCELLED", "NO_SHOW")

# Settings bounds - generous, but enough to catch a typo'd extra zero.
MAX_VISITS_REQUIRED = 100
MAX_EXPIRY_DAYS = 3650
MAX_FIXED_DISCOUNT = Decimal("10000.00")


@dataclass(frozen=True)
class DiscountResult:
    code: DiscountCode
    original_total: Decimal
    discount_amount: Decimal
    new_total: Decimal


@dataclass(frozen=True)
class DiscountFailure:
    reason: str

    @property
    def staff_message(self) -> str:
        return _STAFF_MESSAGES.get(self.reason, GENERIC_FAILURE_MESSAGE)

    @property
    def public_message(self) -> str:
        return _PUBLIC_MESSAGES.get(self.reason, GENERIC_FAILURE_MESSAGE)


class DiscountConfigError(ValueError):
    """An invalid loyalty setting or code definition - a 422 for the owner."""


# --- money -----------------------------------------------------------------


def compute_discount(total: Decimal, discount_type: str, discount_value: Decimal) -> Decimal:
    """The penny discount ``discount_type``/``discount_value`` gives on
    ``total`` - never negative and never more than the total."""
    if discount_type == DISCOUNT_TYPE_PERCENTAGE:
        amount = (total * Decimal(discount_value) / Decimal(100)).quantize(
            PENNY, rounding=ROUND_HALF_UP
        )
    else:
        amount = Decimal(discount_value).quantize(PENNY, rounding=ROUND_HALF_UP)
    return max(Decimal("0.00"), min(amount, total))


def validate_discount(discount_type: str | None, discount_value: Decimal | None) -> None:
    """Same shape as app/payments/money.py::validate_deposit_config."""
    if discount_type not in DISCOUNT_TYPES:
        raise DiscountConfigError("discount_type must be PERCENTAGE or FIXED.")
    if discount_value is None:
        raise DiscountConfigError("discount_value is required.")
    if discount_value <= 0:
        raise DiscountConfigError("discount_value must be greater than zero.")
    if discount_value != discount_value.quantize(PENNY):
        raise DiscountConfigError("discount_value can have at most two decimal places.")
    if discount_type == DISCOUNT_TYPE_PERCENTAGE and discount_value > 100:
        raise DiscountConfigError("A percentage discount cannot exceed 100%.")
    if discount_type == DISCOUNT_TYPE_FIXED and discount_value > MAX_FIXED_DISCOUNT:
        raise DiscountConfigError(f"A fixed discount cannot exceed £{MAX_FIXED_DISCOUNT}.")


def validate_loyalty_settings(
    *, visits_required: int, discount_type: str, discount_value: Decimal, expiry_days: int
) -> None:
    if not 1 <= visits_required <= MAX_VISITS_REQUIRED:
        raise DiscountConfigError(f"visits_required must be between 1 and {MAX_VISITS_REQUIRED}.")
    if not 1 <= expiry_days <= MAX_EXPIRY_DAYS:
        raise DiscountConfigError(f"expiry_days must be between 1 and {MAX_EXPIRY_DAYS}.")
    validate_discount(discount_type, discount_value)


def get_loyalty_settings(garage_id: uuid.UUID) -> LoyaltySettings | None:
    return cast(
        LoyaltySettings | None, LoyaltySettings.query.filter_by(garage_id=garage_id).first()
    )


# --- lookup ------------------------------------------------------------------


def find_code(garage_id: uuid.UUID, raw: str | None, *, lock: bool = False) -> DiscountCode | None:
    """Resolve ``raw`` within ``garage_id`` only - a code never resolves in
    another business, whatever it is."""
    normalised = normalize_code(raw)
    if not is_valid_code(normalised):
        return None
    query = DiscountCode.query.filter_by(garage_id=garage_id, code_normalized=normalised)
    if lock:
        query = query.with_for_update()
    return cast(DiscountCode | None, query.first())


def find_customer_by_email(garage_id: uuid.UUID, email: str | None) -> Customer | None:
    """The customer a public booking with ``email`` will resolve to (see
    app/booking_requests/service.py::resolve_customer_and_vehicle), without
    creating one."""
    if not email:
        return None
    return cast(
        Customer | None,
        Customer.query.filter(Customer.garage_id == garage_id, Customer.email.ilike(email))
        .order_by(Customer.created_at)
        .first(),
    )


def _release_stale_redemptions(code: DiscountCode, now: datetime) -> None:
    """Give back any use still counted against ``code`` whose booking has
    since fallen through - the safety net behind the explicit sync calls."""
    active = DiscountRedemption.query.filter(
        DiscountRedemption.discount_code_id == code.id,
        DiscountRedemption.status.in_(ACTIVE_REDEMPTION_STATUSES),
    ).all()
    for redemption in active:
        if _redemption_is_dead(redemption):
            _set_status(redemption, REDEMPTION_RELEASED, now)


def _redemption_is_dead(redemption: DiscountRedemption) -> bool:
    if redemption.appointment_id is not None:
        appointment = db.session.get(Appointment, redemption.appointment_id)
        return appointment is None or appointment.status in _RELEASING_APPOINTMENT_STATUSES
    if redemption.booking_request_id is not None:
        request = db.session.get(BookingRequest, redemption.booking_request_id)
        return request is None or request.status in _TERMINAL_REQUEST_STATUSES
    return True


def _active_uses(code: DiscountCode, *, exclude: Iterable[uuid.UUID] = ()) -> Any:
    query = DiscountRedemption.query.filter(
        DiscountRedemption.discount_code_id == code.id,
        DiscountRedemption.status.in_(ACTIVE_REDEMPTION_STATUSES),
    )
    excluded = list(exclude)
    if excluded:
        query = query.filter(DiscountRedemption.id.notin_(excluded))
    return query


def evaluate_code(
    garage,
    code: str | None,
    *,
    customer: Customer | None,
    appointment_type=None,
    total: Decimal | None,
    lock: bool = False,
    exclude_redemptions: Iterable[uuid.UUID] = (),
    now: datetime | None = None,
) -> DiscountResult | DiscountFailure:
    """Whether ``code`` applies to a booking of ``total`` for ``customer``
    at ``garage``, and what it is worth.

    ``total`` is the booking's pre-discount total (base price plus add-ons).
    ``lock`` takes a row lock on the code so the limits checked here still
    hold when the caller redeems it in the same transaction (always pass it
    on a path that goes on to call :func:`redeem`). ``appointment_type`` is
    accepted for the shared signature; codes are not restricted by service
    in v1.
    """
    del appointment_type  # no per-service restrictions yet - see docstring
    now = now or datetime.now(UTC)
    # Price checks come before the lookup so their clearer messages reveal
    # nothing about whether a code exists.
    if total is None:
        return DiscountFailure(UNPRICED)
    if total <= 0:
        return DiscountFailure(NOTHING_TO_DISCOUNT)

    found = find_code(garage.id, code, lock=lock)
    if found is None:
        return DiscountFailure(NOT_FOUND)
    if found.status == DISCOUNT_STATUS_DISABLED:
        return DiscountFailure(DISABLED)
    if found.valid_from is not None and found.valid_from > now:
        return DiscountFailure(NOT_YET_VALID)
    if found.expires_at is not None and found.expires_at <= now:
        return DiscountFailure(EXPIRED)
    if found.source == DISCOUNT_SOURCE_LOYALTY and (
        customer is None or found.customer_id != customer.id
    ):
        return DiscountFailure(WRONG_CUSTOMER)

    _release_stale_redemptions(found, now)
    if (
        found.max_uses is not None
        and _active_uses(found, exclude=exclude_redemptions).count() >= found.max_uses
    ):
        return DiscountFailure(USED_UP)
    if found.max_uses_per_customer is not None and customer is not None:
        used_by_customer = (
            _active_uses(found, exclude=exclude_redemptions)
            .filter(DiscountRedemption.customer_id == customer.id)
            .count()
        )
        if used_by_customer >= found.max_uses_per_customer:
            return DiscountFailure(CUSTOMER_LIMIT)

    amount = compute_discount(total, found.discount_type, found.discount_value)
    return DiscountResult(
        code=found, original_total=total, discount_amount=amount, new_total=total - amount
    )


def log_rejection(garage_id, failure: DiscountFailure, *, channel: str) -> None:
    """Counts and reasons only - never the code a customer typed."""
    current_app.logger.info(
        "DISCOUNT_CODE_REJECTED garage=%s channel=%s reason=%s",
        garage_id,
        channel,
        failure.reason,
    )


# --- redemption ----------------------------------------------------------------


def apply_snapshot(target: BookingRequest | Appointment, result: DiscountResult) -> None:
    """Freeze what was applied onto the booking (see
    app/models/loyalty/snapshot.py)."""
    target.discount_code_id = result.code.id
    target.discount_code = result.code.code
    target.discount_type = result.code.discount_type
    target.discount_value = result.code.discount_value
    target.discount_amount = result.discount_amount
    target.price_before_discount = result.original_total


def clear_snapshot(target: BookingRequest | Appointment) -> None:
    target.discount_code_id = None
    target.discount_code = None
    target.discount_type = None
    target.discount_value = None
    target.discount_amount = None
    target.price_before_discount = None


def copy_snapshot(source: BookingRequest | Appointment, target: BookingRequest | Appointment):
    target.discount_code_id = source.discount_code_id
    target.discount_code = source.discount_code
    target.discount_type = source.discount_type
    target.discount_value = source.discount_value
    target.discount_amount = source.discount_amount
    target.price_before_discount = source.price_before_discount


def reprice_snapshot(
    target: BookingRequest | Appointment, pre_discount_total: Decimal | None
) -> Decimal | None:
    """Re-apply a booking's *frozen* discount to a new pre-discount total
    (staff changed its add-ons or service) and return the new discounted
    price. A percentage discount stays that percentage of the new total; a
    fixed one stays that amount, capped at the new total. The code itself is
    not re-checked - it was already redeemed for this booking."""
    if target.discount_type is None or target.discount_value is None:
        return pre_discount_total
    target.price_before_discount = pre_discount_total
    if pre_discount_total is None:
        target.discount_amount = None
        return None
    amount = compute_discount(pre_discount_total, target.discount_type, target.discount_value)
    target.discount_amount = amount
    redemption = redemption_for(target)
    if redemption is not None:
        redemption.amount = amount
    return pre_discount_total - amount


def redeem(
    garage,
    result: DiscountResult,
    *,
    customer_id: uuid.UUID | None,
    status: str,
    booking_request: BookingRequest | None = None,
    appointment: Appointment | None = None,
    now: datetime | None = None,
) -> DiscountRedemption | DiscountFailure:
    """Record one use of an evaluated (and row-locked) code. The partial
    unique index is the backstop for single-use codes: if it fires anyway,
    this reports the code as used rather than failing the booking with a
    500."""
    now = now or datetime.now(UTC)
    redemption = DiscountRedemption(
        garage_id=garage.id,
        discount_code_id=result.code.id,
        booking_request_id=booking_request.id if booking_request is not None else None,
        appointment_id=appointment.id if appointment is not None else None,
        customer_id=customer_id,
        amount=result.discount_amount,
        status=status,
        single_use=result.code.max_uses == 1,
        reserved_at=now,
        consumed_at=now if status == REDEMPTION_CONSUMED else None,
    )
    try:
        with db.session.begin_nested():
            db.session.add(redemption)
    except IntegrityError:
        return DiscountFailure(USED_UP)
    return redemption


def redemption_for(target: BookingRequest | Appointment) -> DiscountRedemption | None:
    """The most recent redemption belonging to ``target``."""
    if target.id is None:
        return None
    column = (
        DiscountRedemption.appointment_id
        if isinstance(target, Appointment)
        else DiscountRedemption.booking_request_id
    )
    return cast(
        DiscountRedemption | None,
        DiscountRedemption.query.filter(column == target.id)
        .order_by(DiscountRedemption.created_at.desc())
        .first(),
    )


def _set_status(redemption: DiscountRedemption, status: str, now: datetime) -> None:
    if redemption.status == status:
        return
    redemption.status = status
    if status == REDEMPTION_CONSUMED:
        redemption.consumed_at = now
        redemption.released_at = None
    elif status == REDEMPTION_RELEASED:
        redemption.released_at = now
    else:
        redemption.released_at = None


def _move_to(redemption: DiscountRedemption, status: str, now: datetime) -> None:
    """Move a redemption, refusing to bring a RELEASED single-use use back
    to life if someone else has used the code since - in that case it stays
    released and the booking simply keeps the price it already agreed."""
    if redemption.status == REDEMPTION_RELEASED and status in ACTIVE_REDEMPTION_STATUSES:
        code = (
            DiscountCode.query.filter_by(id=redemption.discount_code_id).with_for_update().first()
        )
        if (
            code is not None
            and code.max_uses is not None
            and _active_uses(code).count() >= code.max_uses
        ):
            current_app.logger.warning(
                "DISCOUNT_REDEMPTION_NOT_REACTIVATED redemption=%s reason=used_since",
                redemption.id,
            )
            return
    _set_status(redemption, status, now)


def sync_booking_request_redemption(
    booking_request: BookingRequest, now: datetime | None = None
) -> None:
    """Bring a request's redemption in line with the request's status. Call
    after changing the status, before committing. A no-op without a code."""
    if booking_request.discount_code_id is None:
        return
    redemption = redemption_for(booking_request)
    if redemption is None or redemption.appointment_id is not None:
        # Once approved, the appointment owns the redemption's lifecycle.
        return
    now = now or datetime.now(UTC)
    status = booking_request.status
    if status in ("AWAITING_PAYMENT", "AWAITING_TERMS"):
        _move_to(redemption, REDEMPTION_RESERVED, now)
    elif status in ("PENDING", "APPROVED"):
        _move_to(redemption, REDEMPTION_CONSUMED, now)
    elif status in _TERMINAL_REQUEST_STATUSES:
        _set_status(redemption, REDEMPTION_RELEASED, now)


def release_for_booking_requests(request_ids: Iterable[uuid.UUID]) -> None:
    """Release the redemptions of requests a bulk UPDATE just expired."""
    ids = list(request_ids)
    if not ids:
        return
    now = datetime.now(UTC)
    for redemption in DiscountRedemption.query.filter(
        DiscountRedemption.booking_request_id.in_(ids),
        DiscountRedemption.appointment_id.is_(None),
        DiscountRedemption.status.in_(ACTIVE_REDEMPTION_STATUSES),
    ).all():
        _set_status(redemption, REDEMPTION_RELEASED, now)


def sync_appointment_redemption(appointment: Appointment, now: datetime | None = None) -> None:
    """A cancelled or no-show appointment gives its code back; reactivating
    it takes the code again if it is still free."""
    if appointment.discount_code_id is None:
        return
    redemption = redemption_for(appointment)
    if redemption is None:
        return
    now = now or datetime.now(UTC)
    if appointment.status in _RELEASING_APPOINTMENT_STATUSES:
        _set_status(redemption, REDEMPTION_RELEASED, now)
    else:
        _move_to(redemption, REDEMPTION_CONSUMED, now)


def attach_redemption_to_appointment(
    booking_request: BookingRequest, appointment: Appointment
) -> None:
    """Approval: the new appointment inherits the request's discount and
    takes over its redemption."""
    if booking_request.discount_code_id is None:
        return
    redemption = redemption_for(booking_request)
    if redemption is not None:
        redemption.appointment_id = appointment.id
        _move_to(redemption, REDEMPTION_CONSUMED, datetime.now(UTC))


def release_appointment_discount(appointment: Appointment) -> None:
    """Staff removed (or replaced) the code on an appointment."""
    redemption = redemption_for(appointment)
    if redemption is not None:
        _set_status(redemption, REDEMPTION_RELEASED, datetime.now(UTC))


# --- listing ----------------------------------------------------------------

STATUS_ACTIVE = "ACTIVE"
STATUS_USED = "USED"
STATUS_EXPIRED = "EXPIRED"
STATUS_DISABLED = "DISABLED"
DERIVED_STATUSES = (STATUS_ACTIVE, STATUS_USED, STATUS_EXPIRED, STATUS_DISABLED)


def _uses_subquery():
    return (
        select(func.count(DiscountRedemption.id))
        .where(
            DiscountRedemption.discount_code_id == DiscountCode.id,
            DiscountRedemption.status.in_(ACTIVE_REDEMPTION_STATUSES),
        )
        .correlate(DiscountCode)
        .scalar_subquery()
    )


def derived_status_expression(now: datetime):
    """Disabled > Used > Expired > Active - SQL, so the list can filter and
    paginate on it server-side."""
    uses = _uses_subquery()
    return case(
        (DiscountCode.status == DISCOUNT_STATUS_DISABLED, STATUS_DISABLED),
        (
            and_(DiscountCode.max_uses.isnot(None), uses >= DiscountCode.max_uses),
            STATUS_USED,
        ),
        (
            and_(DiscountCode.expires_at.isnot(None), DiscountCode.expires_at <= now),
            STATUS_EXPIRED,
        ),
        else_=STATUS_ACTIVE,
    )


def list_codes(
    garage_id: uuid.UUID,
    *,
    source: str | None = None,
    status: str | None = None,
    search: str | None = None,
    customer_id: uuid.UUID | None = None,
    limit: int = 25,
    offset: int = 0,
    now: datetime | None = None,
) -> tuple[list[tuple[DiscountCode, int, str]], int]:
    """``([(code, uses, derived_status), ...], total)`` newest first."""
    now = now or datetime.now(UTC)
    uses = _uses_subquery().label("uses")
    derived = derived_status_expression(now).label("derived_status")
    query = (
        db.session.query(DiscountCode, uses, derived)
        .outerjoin(Customer, Customer.id == DiscountCode.customer_id)
        .filter(DiscountCode.garage_id == garage_id)
    )
    if source:
        query = query.filter(DiscountCode.source == source)
    if customer_id is not None:
        query = query.filter(DiscountCode.customer_id == customer_id)
    if status:
        query = query.filter(derived_status_expression(now) == status)
    if search and search.strip():
        term = search.strip()
        like = f"%{term}%"
        normalised = normalize_code(term)
        conditions: list[Any] = [
            Customer.first_name.ilike(like),
            Customer.last_name.ilike(like),
            Customer.email.ilike(like),
            func.concat(Customer.first_name, " ", Customer.last_name).ilike(like),
        ]
        if normalised:
            conditions.append(DiscountCode.code_normalized.contains(normalised))
        query = query.filter(or_(*conditions))
    total = query.count()
    rows = (
        query.order_by(DiscountCode.created_at.desc(), DiscountCode.id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [(row[0], int(row[1] or 0), row[2]) for row in rows], total


def code_uses(code: DiscountCode) -> int:
    return int(_active_uses(code).count())


def code_status(code: DiscountCode, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if code.status == DISCOUNT_STATUS_DISABLED:
        return STATUS_DISABLED
    if code.max_uses is not None and code_uses(code) >= code.max_uses:
        return STATUS_USED
    if code.expires_at is not None and code.expires_at <= now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE
