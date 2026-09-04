"""UK-aware mobile number parsing.

Customers type a phone number the way they normally would - `07123 456789`,
`+44 7123 456789`, `0044 7123456789`, with or without spaces - and this
normalises it to E.164 (`+447123456789`) for storage, which is the format
Twilio (and everything else in the SMS world) expects. Nobody should ever be
asked to type the `+44` themselves.

Only the public booking form calls :func:`normalize_uk_mobile` today (see
``app/public_booking/schemas.py::UKMobileField``) - staff-entered customer
phone numbers stay free-text for now, since that field is optional and used
more loosely than the public form's "how do we text you" field.
"""

import phonenumbers


class InvalidPhoneNumberError(ValueError):
    """Raised with a message safe to show the customer directly."""


def normalize_uk_mobile(raw: str) -> str:
    """Parse ``raw`` as a UK number and return it in E.164 form.

    Accepts national (``07…``) or international (``+44…`` / ``0044…``) input.
    Raises :class:`InvalidPhoneNumberError` for anything that isn't a
    plausible UK mobile number - not just "unparseable", so a landline typed
    into the mobile-number field gets a clear, specific error rather than a
    generic parse failure.
    """
    raw = (raw or "").strip()
    if not raw:
        raise InvalidPhoneNumberError("Enter a mobile number.")

    try:
        parsed = phonenumbers.parse(raw, "GB")
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(
            "Enter a valid UK mobile number, e.g. 07123 456789."
        ) from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError(
            "Enter a valid UK mobile number, e.g. 07123 456789."
        )

    number_type = phonenumbers.number_type(parsed)
    # FIXED_LINE_OR_MOBILE covers UK ranges phonenumbers can't split further
    # (common after number porting) - only a definite landline is rejected.
    if number_type == phonenumbers.PhoneNumberType.FIXED_LINE:
        raise InvalidPhoneNumberError(
            "That looks like a landline number - please enter a mobile "
            "number so we can text you about your booking."
        )

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
