"""Turn a raw Twilio status / error code into something a garage owner can act on.

Twilio's `MessageStatus` on its own ("undelivered", "failed") tells staff
nothing about *why* a WhatsApp message did not arrive - and the real reason
(the customer isn't on WhatsApp, or it's been more than 24 hours since they
last messaged so an approved template is required) is exactly what decides
what the business should do next. Twilio always sends an `ErrorCode` with a
failed delivery; this module maps the ones that actually occur in production
to a plain-English explanation, and provides a single short status label the
Communications UI can show instead of the bare provider string.

Nothing here talks to Twilio - it's pure lookup, used both when a send fails
synchronously (service.py) and when an async status callback lands
(whatsapp_webhooks.py).
"""

from __future__ import annotations

# Twilio messaging error codes -> business-facing explanation. Sources:
# Twilio "Messaging" and "WhatsApp" error dictionaries. Only codes that
# meaningfully change what staff should do are listed; anything else falls
# back to a generic message keyed on the delivery status.
_ERROR_EXPLANATIONS: dict[int, str] = {
    # --- WhatsApp 24-hour customer-service window -------------------------
    63016: (
        "Not delivered — it's been more than 24 hours since this customer last "
        "messaged, so WhatsApp only allows an approved template message, not "
        "free text."
    ),
    # --- Recipient not usable on WhatsApp --------------------------------
    63003: "Not delivered — this number is not reachable on WhatsApp.",
    63024: "Not delivered — this number is not a valid WhatsApp recipient.",
    63013: ("Not delivered — WhatsApp blocked this message under its business messaging policy."),
    63021: "Not delivered — the recipient's device rejected the message.",
    63015: (
        "Not delivered — the WhatsApp sender is still in sandbox mode and this "
        "number has not joined it."
    ),
    63018: "Not delivered — WhatsApp rate limit reached; try again shortly.",
    63005: "Not delivered — WhatsApp did not accept this message's content.",
    # --- Sender / account configuration --------------------------------
    63007: (
        "Not delivered — this business's WhatsApp sender is not configured correctly at Twilio."
    ),
    63002: ("Not delivered — this business's WhatsApp sender number was not recognised by Twilio."),
    # --- Bad destination number ---------------------------------------
    21211: "Not delivered — the phone number is not valid.",
    21408: ("Not delivered — this Twilio account is not enabled to message that country."),
    21610: "Not delivered — this customer has replied STOP and is unsubscribed.",
    21612: "Not delivered — that number cannot receive messages from this sender.",
    # --- Template-specific -------------------------------------------
    63051: "Not delivered — the WhatsApp template was rejected by Meta.",
    63049: "Not delivered — the WhatsApp template is not approved for this recipient.",
}

# Codes that specifically mean "you need an approved template to reach this
# person right now" - the UI can surface a distinct call to action for these.
TEMPLATE_REQUIRED_CODES = frozenset({63016, 63049, 63051})

_FAILED_STATUSES = frozenset({"failed", "undelivered", "FAILED"})


def _coerce_code(error_code: object) -> int | None:
    if error_code is None or error_code == "":
        return None
    try:
        return int(str(error_code).strip())
    except (TypeError, ValueError):
        return None


def describe_delivery_failure(error_code: object, status: str | None = None) -> str | None:
    """A plain-English reason a message did not arrive, or ``None`` when the
    inputs carry nothing worth explaining (e.g. a normal in-flight status).

    Falls back to a generic-but-honest line when the message clearly failed
    but the code is unknown - never invents a specific cause."""
    code = _coerce_code(error_code)
    if code is not None and code in _ERROR_EXPLANATIONS:
        return _ERROR_EXPLANATIONS[code]

    if status in _FAILED_STATUSES:
        if code is not None:
            return f"Not delivered — WhatsApp/SMS provider error {code}."
        return "Not delivered — the messaging provider did not accept it."

    return None


def template_required(error_code: object) -> bool:
    """True when the failure means a freeform message can't reach this person
    and an approved WhatsApp template is the only way through."""
    return _coerce_code(error_code) in TEMPLATE_REQUIRED_CODES


_STATUS_LABELS: dict[str, str] = {
    "queued": "Queued",
    "accepted": "Queued",
    "scheduled": "Scheduled",
    "sending": "Sending",
    "sent": "Sent",
    "delivered": "Delivered",
    "read": "Read",
    "receiving": "Receiving",
    "received": "Received",
    "SKIPPED_NOT_CONFIGURED": "Not sent — messaging isn't connected",
}


def delivery_status_label(status: str | None, error_code: object = None) -> str:
    """One short business-facing status for an outbound row. For a failure
    this is the specific reason where we have one, otherwise a clear
    "Not delivered"; for anything in-flight or successful it's a tidy label
    for the raw Twilio status."""
    if status in _FAILED_STATUSES:
        return describe_delivery_failure(error_code, status) or "Not delivered"
    if status in _STATUS_LABELS:
        return _STATUS_LABELS[status]
    return status or "Unknown"
