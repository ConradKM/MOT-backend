"""Pure unit tests for deposit calculation/validation - no Flask app or
database needed (app.payments.money has no side effects)."""

from decimal import Decimal

import pytest

from app.payments.money import (
    DepositConfigError,
    calculate_deposit_minor,
    minor_to_decimal,
    validate_deposit_config,
)


def test_fixed_deposit_calculates_in_minor_units():
    minor = calculate_deposit_minor(
        deposit_type="FIXED", deposit_value=Decimal("20.00"), base_price=Decimal("100.00")
    )
    assert minor == 2000


def test_percentage_deposit_calculates_in_minor_units():
    minor = calculate_deposit_minor(
        deposit_type="PERCENTAGE", deposit_value=Decimal(25), base_price=Decimal("100.00")
    )
    assert minor == 2500


def test_percentage_deposit_rounds_to_nearest_penny():
    # 33% of £54.85 = 18.1005 -> rounds (half-up) to 18.10 -> 1810p.
    minor = calculate_deposit_minor(
        deposit_type="PERCENTAGE", deposit_value=Decimal(33), base_price=Decimal("54.85")
    )
    assert minor == 1810
    # Sanity: recompute independently with Decimal to confirm no float ever entered.
    expected_major = (Decimal("54.85") * Decimal(33) / Decimal(100)).quantize(
        Decimal("0.01"), rounding="ROUND_HALF_UP"
    )
    assert minor == int(expected_major * 100)


def test_minor_to_decimal_round_trips():
    assert minor_to_decimal(2500) == Decimal("25.00")
    assert minor_to_decimal(1) == Decimal("0.01")


def test_validate_deposit_config_noop_when_not_required():
    validate_deposit_config(
        deposit_required=False, deposit_type=None, deposit_value=None, base_price=None
    )  # does not raise


def test_validate_deposit_config_requires_type_and_value():
    with pytest.raises(DepositConfigError):
        validate_deposit_config(
            deposit_required=True, deposit_type=None, deposit_value=None, base_price=Decimal(100)
        )


def test_validate_deposit_config_rejects_non_positive_value():
    with pytest.raises(DepositConfigError):
        validate_deposit_config(
            deposit_required=True,
            deposit_type="FIXED",
            deposit_value=Decimal(0),
            base_price=Decimal(100),
        )


def test_validate_deposit_config_rejects_percentage_over_100():
    with pytest.raises(DepositConfigError):
        validate_deposit_config(
            deposit_required=True,
            deposit_type="PERCENTAGE",
            deposit_value=Decimal(150),
            base_price=Decimal(100),
        )


def test_validate_deposit_config_rejects_percentage_with_no_price():
    with pytest.raises(DepositConfigError):
        validate_deposit_config(
            deposit_required=True,
            deposit_type="PERCENTAGE",
            deposit_value=Decimal(25),
            base_price=None,
        )


def test_validate_deposit_config_rejects_fixed_deposit_exceeding_price():
    with pytest.raises(DepositConfigError):
        validate_deposit_config(
            deposit_required=True,
            deposit_type="FIXED",
            deposit_value=Decimal(150),
            base_price=Decimal(100),
        )


def test_validate_deposit_config_allows_fixed_deposit_with_unknown_price():
    # A fixed £20 deposit is meaningful even if the service has no listed
    # price yet - only a *percentage* deposit needs one to calculate from.
    validate_deposit_config(
        deposit_required=True, deposit_type="FIXED", deposit_value=Decimal(20), base_price=None
    )
