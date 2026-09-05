"""Business-specific wording for transactional messages (Part 24/25).

Deliberately the simplest thing that satisfies "OWNER can edit safe text,
never code": a fixed set of named templates with ``{{variable}}``
placeholders, substituted by straight string replacement against an
allow-listed set of values the caller provides - never ``str.format(**...)``
or any templating engine capable of executing arbitrary expressions. An
unrecognised ``{{...}}`` in a template (a typo, or a variable this event
doesn't have) is left as literal text rather than raising, so a bad edit
degrades to visibly-odd wording instead of a crash or a blank message.
"""

from __future__ import annotations

import re

from app.extensions import db
from app.models.communications.message_template import GarageMessageTemplate

BOOKING_ACKNOWLEDGEMENT = "booking_acknowledgement"
BOOKING_CONFIRMATION = "booking_confirmation"
BOOKING_REJECTED = "booking_rejected"
APPOINTMENT_REMINDER = "appointment_reminder"
APPOINTMENT_CANCELLED = "appointment_cancelled"
APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
MISSED_CALL_ACK = "missed_call_ack"

TEMPLATE_KEYS = (
    BOOKING_ACKNOWLEDGEMENT,
    BOOKING_CONFIRMATION,
    BOOKING_REJECTED,
    APPOINTMENT_REMINDER,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_RESCHEDULED,
    MISSED_CALL_ACK,
)

DEFAULT_TEMPLATES: dict[str, str] = {
    BOOKING_ACKNOWLEDGEMENT: (
        "Thanks for your booking request with {{business_name}}. We've received "
        "your request for {{appointment_type}} on {{appointment_date}}"
        "{{appointment_time_suffix}}. We'll confirm shortly."
    ),
    BOOKING_CONFIRMATION: (
        "Your {{appointment_type}} booking with {{business_name}} is confirmed "
        "for {{appointment_date}} at {{appointment_time}}."
    ),
    BOOKING_REJECTED: (
        "We're sorry, {{business_name}} isn't able to confirm your requested "
        "{{appointment_type}} booking on {{appointment_date}}. Please get in "
        "touch to find another time."
    ),
    APPOINTMENT_REMINDER: (
        "Reminder: your {{appointment_type}} appointment with {{business_name}} "
        "is on {{appointment_date}} at {{appointment_time}}."
    ),
    APPOINTMENT_CANCELLED: (
        "Your appointment with {{business_name}} on {{appointment_date}} has "
        "been cancelled."
    ),
    APPOINTMENT_RESCHEDULED: (
        "Your appointment with {{business_name}} has been moved to "
        "{{appointment_date}} at {{appointment_time}}."
    ),
    MISSED_CALL_ACK: (
        "Sorry we missed your call to {{business_name}}. You can reply here "
        "to book or manage an appointment."
    ),
}

# Every variable name any default template (or a safe owner edit) may use.
# render_template() only ever substitutes from this set - anything else in
# **variables is ignored, and any {{name}} in the body outside this set is
# left as literal text.
ALLOWED_VARIABLES = {
    "business_name",
    "customer_first_name",
    "appointment_type",
    "appointment_date",
    "appointment_time",
    "appointment_time_suffix",
}

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _resolve_body(garage, key: str) -> str:
    custom = GarageMessageTemplate.query.filter_by(garage_id=garage.id, key=key).first()
    if custom is not None:
        return custom.body
    return DEFAULT_TEMPLATES.get(key, "")


def render_body(body: str, **variables: str) -> str:
    """The substitution rule itself, given raw template text directly -
    split out from render_template so the owner-facing preview endpoint can
    render an unsaved draft the customer will never actually receive,
    without writing it to the database first."""
    safe_values = {k: v for k, v in variables.items() if k in ALLOWED_VARIABLES and v is not None}

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        return str(safe_values.get(name, match.group(0) if name not in ALLOWED_VARIABLES else ""))

    return _PLACEHOLDER.sub(_replace, body)


def render_template(garage, key: str, **variables: str) -> str:
    return render_body(_resolve_body(garage, key), **variables)


def get_template_body(garage, key: str) -> tuple[str, bool]:
    """``(body, is_custom)`` - the text an OWNER would see/edit for one
    template key, and whether it's their own override or the CoMaz OS
    default."""
    custom = GarageMessageTemplate.query.filter_by(garage_id=garage.id, key=key).first()
    if custom is not None:
        return custom.body, True
    return DEFAULT_TEMPLATES.get(key, ""), False


def set_template_body(garage, key: str, body: str) -> GarageMessageTemplate:
    """Saves the owner's override for one template key, replacing any
    previous override (never more than one row per garage/key - enforced by
    the table's own unique constraint, mirrored here as find-or-create)."""
    row = GarageMessageTemplate.query.filter_by(garage_id=garage.id, key=key).first()
    if row is None:
        row = GarageMessageTemplate(garage_id=garage.id, key=key, body=body)
        db.session.add(row)
    else:
        row.body = body
    db.session.commit()
    return row


def reset_template_body(garage, key: str) -> None:
    """Removes the owner's override for one key, reverting it to the CoMaz
    OS default text. A no-op if there was no override."""
    GarageMessageTemplate.query.filter_by(garage_id=garage.id, key=key).delete()
    db.session.commit()


# Representative sample values so an owner can preview a template's wording
# without a real booking/appointment on hand - never sent to a customer.
PREVIEW_VARIABLES: dict[str, str] = {
    "business_name": "Your Business",
    "customer_first_name": "Jane",
    "appointment_type": "MOT Test",
    "appointment_date": "12 September 2026",
    "appointment_time": "10:30",
    "appointment_time_suffix": " at 10:30",
}


def preview_body(body: str) -> str:
    return render_body(body, **PREVIEW_VARIABLES)
