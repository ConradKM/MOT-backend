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
        raise InvalidPhoneNumberError("Enter a valid UK mobile number, e.g. 07123 456789.") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError("Enter a valid UK mobile number, e.g. 07123 456789.")

    number_type = phonenumbers.number_type(parsed)
    # FIXED_LINE_OR_MOBILE covers UK ranges phonenumbers can't split further
    # (common after number porting) - only a definite landline is rejected.
    if number_type == phonenumbers.PhoneNumberType.FIXED_LINE:
        raise InvalidPhoneNumberError(
            "That looks like a landline number - please enter a mobile "
            "number so we can text you about your booking."
        )

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_uk_phone(raw: str) -> str:
    """E.164 for any valid UK number - mobile **or** landline. Used for a
    staff-placed outbound call, which can legitimately dial a landline (the
    mobile-only :func:`normalize_uk_mobile` is for the "how do we text you"
    field)."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidPhoneNumberError("Enter a phone number.")
    try:
        parsed = phonenumbers.parse(raw, "GB")
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError("Enter a valid UK phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError("Enter a valid UK phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def phone_from_sip_uri(value: str | None) -> str | None:
    """The phone-number portion of a ``sip:+18005551212@host`` / ``tel:...``
    URI - or of a bare number, which is returned as-is.

    Real SIP headers are routinely the RFC 3261 name-addr form - the URI
    wrapped in ``<...>``, with parameters like ``;tag=`` *outside* the
    brackets, e.g. ``<sip:+441234567890@host>;tag=abc`` - so that wrapper is
    stripped first. The result is untrusted caller/network metadata: it may
    only ever drive a lookup, never an authorisation decision by itself."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        value = value[1:end] if end != -1 else value[1:]
    for prefix in ("sips:", "sip:", "tel:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    number = value.split("@", 1)[0].split(";", 1)[0]
    return number or None


def e164_from_address(raw: str | None) -> str | None:
    """E.164 for a number as Twilio reports it on a call - ``+44...`` for a
    PSTN call, possibly ``sip:+44...@domain`` or a carrier's national/
    ``44...`` form for one delivered over SIP. ``None`` when it isn't a
    number at all (withheld, ``anonymous``, a client identity).

    Falls back to the value as-is when it's already a well-formed
    ``+<digits>`` string that ``normalize_uk_phone`` itself rejects - e.g. a
    number outside any range currently allocated by Ofcom, which is exactly
    what a test/reserved caller ID (the ``+447700900xxx`` drama range) is.
    Twilio itself does no such allocation check on the caller id it reports,
    so neither should this."""
    number = phone_from_sip_uri(raw)
    if not number:
        return None
    digits = number.lstrip("+")
    if not number.startswith("+") and digits.isdigit() and digits.startswith("44"):
        # Carriers commonly send the UK DID as 44xxxxxxxxxx with no '+';
        # parsed with a GB default that would read as a national number.
        number = f"+{digits}"
    try:
        return normalize_uk_phone(number)
    except InvalidPhoneNumberError:
        if number.startswith("+") and number[1:].isdigit():
            return number
        return None
