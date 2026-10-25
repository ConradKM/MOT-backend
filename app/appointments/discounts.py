"""Discount codes on staff-managed appointments.

Shared by the appointment create/update routes and the staff "preview this
code" endpoint (app/loyalty/routes.py), so the form's live price and the
saved price can never disagree.
"""

from __future__ import annotations

from decimal import Decimal

from flask_smorest import abort

from app.appointments.add_ons.service import price_delta
from app.loyalty.service import (
    DiscountFailure,
    DiscountResult,
    evaluate_code,
    log_rejection,
    redemption_for,
)
from app.models.appointments.appointment import Appointment


def booked_base_price(appointment: Appointment) -> Decimal | None:
    """The service's base price as it was booked - recovered from the
    snapshot, never today's catalogue. On a discounted appointment
    ``price_at_booking`` is the *discounted* total, so the base comes from
    ``price_before_discount``; using ``price_at_booking`` there would quietly
    undo (or compound) the discount on the next edit."""
    if appointment.discount_code_id is not None or appointment.price_before_discount is not None:
        pre = appointment.price_before_discount
    else:
        pre = appointment.price_at_booking
    if pre is None:
        return None
    return pre - price_delta(appointment.add_ons)


def pre_discount_total(appointment: Appointment) -> Decimal | None:
    if appointment.price_before_discount is not None:
        return appointment.price_before_discount
    return appointment.price_at_booking


def evaluate_for_staff(
    garage,
    code: str,
    *,
    customer,
    appointment_type,
    total: Decimal | None,
    appointment: Appointment | None = None,
    lock: bool = False,
) -> DiscountResult:
    """``evaluate_code`` for an authenticated staff member - who gets the
    specific reason (they can see the codes anyway) - aborting 422 on
    failure."""
    exclude = []
    if appointment is not None:
        existing = redemption_for(appointment)
        if existing is not None:
            exclude.append(existing.id)
    result = evaluate_code(
        garage,
        code,
        customer=customer,
        appointment_type=appointment_type,
        total=total,
        lock=lock,
        exclude_redemptions=exclude,
    )
    if isinstance(result, DiscountFailure):
        log_rejection(garage.id, result, channel="staff")
        abort(
            422,
            message=result.staff_message,
            errors={"json": {"discount_code": [result.staff_message]}, "reason": result.reason},
        )
    assert isinstance(result, DiscountResult)
    return result
