"""Unit tests for the shared UK mobile-number normaliser.

app/phone.py has no HTTP surface of its own (app/public_booking/schemas.py
is its only caller today, soon joined by app/communications/service.py) - it
is tested directly here rather than through an API endpoint.
"""

import pytest

from app.phone import InvalidPhoneNumberError, normalize_uk_mobile


@pytest.mark.parametrize(
    "raw",
    [
        "07123 456789",
        "07123456789",
        "+44 7123 456789",
        "+447123456789",
        "0044 7123 456789",
        "0044-7123-456789",
    ],
)
def test_normalize_uk_mobile_accepts_common_formats(raw):
    assert normalize_uk_mobile(raw) == "+447123456789"


def test_normalize_uk_mobile_rejects_landline():
    with pytest.raises(InvalidPhoneNumberError, match="landline"):
        normalize_uk_mobile("020 7946 0958")


def test_normalize_uk_mobile_rejects_empty():
    with pytest.raises(InvalidPhoneNumberError, match="Enter a mobile number"):
        normalize_uk_mobile("")


def test_normalize_uk_mobile_rejects_garbage():
    with pytest.raises(InvalidPhoneNumberError):
        normalize_uk_mobile("not a number")
