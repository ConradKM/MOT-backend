"""Feedback support-notification email.

A failure here must never lose an already-saved feedback row - this is only
ever called from app/feedback/routes.py *after* the DB commit, and any
exception raised by send_email (a stub/misconfigured provider, or a genuine
Resend failure - see app/email/__init__.py) is caught and logged here rather
than propagated, so the HTTP response to the business user still reflects
that their feedback was saved.
"""

import logging

from flask import current_app

from app.email import send_email
from app.models.feedback import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    TYPE_BUG,
    TYPE_COMPLIMENT,
    TYPE_GENERAL,
    TYPE_NOT_WORKING,
    TYPE_OTHER,
    TYPE_SUGGESTION,
    Feedback,
)

logger = logging.getLogger(__name__)

_TYPE_LABELS = {
    TYPE_GENERAL: "General Feedback",
    TYPE_SUGGESTION: "Suggestion / Feature Request",
    TYPE_BUG: "Bug / Technical Issue",
    TYPE_NOT_WORKING: "Something Isn't Working",
    TYPE_COMPLIMENT: "Compliment",
    TYPE_OTHER: "Other",
}
_PRIORITY_LABELS = {PRIORITY_LOW: "Low", PRIORITY_NORMAL: "Normal", PRIORITY_HIGH: "High"}


def send_feedback_notification(feedback: Feedback) -> None:
    """Best-effort email to the support inbox - see module docstring."""
    lines = [
        "New Business Feedback",
        "",
        "Business:",
        feedback.business_name,
        "",
        "Submitted by:",
        feedback.user_email,
        "",
        "Feedback Type:",
        _TYPE_LABELS.get(feedback.type, feedback.type),
        "",
        "Priority:",
        _PRIORITY_LABELS.get(feedback.priority, feedback.priority),
    ]
    if feedback.subject:
        lines += ["", "Subject:", feedback.subject]
    lines += [
        "",
        "Message:",
        feedback.message,
        "",
        "Submitted:",
        feedback.created_at.strftime("%d %B %Y"),
    ]

    try:
        send_email(
            to=current_app.config["FEEDBACK_NOTIFICATION_EMAIL"],
            subject=f"New feedback from {feedback.business_name}",
            body="\n".join(lines),
            reply_to=feedback.user_email,
        )
    except Exception:
        logger.exception("[feedback] notification email failed for feedback_id=%s", feedback.id)
