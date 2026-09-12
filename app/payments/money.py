"""Deposit configuration validation and calculation.

Every amount here is either a ``Decimal`` (major units, e.g. GBP pounds - what
a person reads and types) or an ``int`` of minor units (pence - what gets
sent to the payment provider and stored on BookingPayment.amount_minor).
Floats never appear: a float deposit calculation is exactly the kind of bug
that quietly overcharges or undercharges a customer by a penny.
"""

from decimal import ROUND_HALF_UP, Decimal

PENCE_PER_POUND = Decimal(100)


class DepositConfigError(ValueError):
    """A deposit configuration (on an AppointmentType) is invalid - surfaced
    as a 422 field error by the appointment-types routes."""


def validate_deposit_config(
    *,
    deposit_required: bool,
    deposit_type: str | None,
    deposit_value: Decimal | None,
    base_price: Decimal | None,
) -> None:
    """Validate one proposed (deposit_required, deposit_type, deposit_value)
    combination against the type's base_price. Raises DepositConfigError with
    a customer/owner-facing message; a no-op when deposit_required is False.

    Deliberately takes the *fully resolved* proposed state (not a partial
    PATCH body) - the appointment-types routes merge PATCH data onto the
    existing row before calling this, so a partial update can't leave an
    inconsistent combination on the model.
    """
    if not deposit_required:
        return

    if deposit_type not in ("FIXED", "PERCENTAGE"):
        raise DepositConfigError("deposit_type must be FIXED or PERCENTAGE when a deposit is required.")

    if deposit_value is None:
        raise DepositConfigError("deposit_value is required when a deposit is required.")

    if deposit_value <= 0:
        raise DepositConfigError("deposit_value must be greater than zero.")

    if deposit_type == "PERCENTAGE":
        if deposit_value > 100:
            raise DepositConfigError("A percentage deposit cannot exceed 100%.")
        if base_price is None:
            raise DepositConfigError(
                "Set a service price before configuring a percentage deposit - "
                "a percentage deposit needs a price to calculate from."
            )
    else:  # FIXED
        if base_price is not None and deposit_value > base_price:
            raise DepositConfigError("A fixed deposit cannot exceed the service price.")


def calculate_deposit_minor(
    *,
    deposit_type: str,
    deposit_value: Decimal,
    base_price: Decimal | None,
) -> int:
    """The deposit amount in minor units (pence), for a known base_price.

    Rounds to the nearest penny (ROUND_HALF_UP - standard cash rounding).
    Callers must have already validated the configuration (see
    validate_deposit_config); this raises DepositConfigError instead of
    silently producing a nonsensical amount if asked to compute a percentage
    deposit with no base_price, which should never happen given that
    validation.
    """
    if deposit_type == "PERCENTAGE":
        if base_price is None:
            raise DepositConfigError(
                "Cannot calculate a percentage deposit without a service price."
            )
        major = (base_price * deposit_value / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        major = Decimal(deposit_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    minor = int((major * PENCE_PER_POUND).to_integral_value(rounding=ROUND_HALF_UP))
    return minor


def minor_to_decimal(amount_minor: int) -> Decimal:
    return (Decimal(amount_minor) / PENCE_PER_POUND).quantize(Decimal("0.01"))
