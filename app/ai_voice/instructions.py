"""Builds the OpenAI Realtime session's dynamic ``instructions`` (system
prompt) from one business's real CoMaz data - never a hard-coded business
name, address, or service list.

This only seeds the model with a *summary* so it can greet the caller
correctly and avoid an unnecessary first tool round-trip; the model still has
the ``get_business_info``/``get_appointment_types``/``get_available_slots``
tools (see tools.py) to look up anything current or more detailed mid-call.
"""

from __future__ import annotations

from app.conversation import actions

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _hours_summary(garage) -> str:
    hours = actions.get_business_hours(garage)
    lines = []
    for weekday in range(7):
        opens_at, closes_at, is_closed = hours.get(weekday, (None, None, True))
        if is_closed or opens_at is None or closes_at is None:
            lines.append(f"{_WEEKDAY_NAMES[weekday]}: closed")
        else:
            lines.append(f"{_WEEKDAY_NAMES[weekday]}: {opens_at:%H:%M}-{closes_at:%H:%M}")
    return "; ".join(lines)


def _services_summary(garage) -> str:
    types = actions.get_appointment_types(garage)
    if not types:
        return "No services are configured yet - offer to take a callback request instead."
    names = ", ".join(t.name for t in types)
    return f"Services offered: {names}."


def build_instructions(garage) -> str:
    """The full system prompt for one call. Rebuilt fresh per call (never
    cached) so a same-day change to hours/services is reflected immediately."""
    return (
        f"You are the phone assistant for {garage.name}, a UK vehicle garage business using "
        "CoMaz OS. You are answering a real inbound customer phone call - be warm, concise, "
        "and speak naturally, as a helpful front-of-house receptionist would, not like an IVR "
        "menu. Use British English and a friendly, professional tone. Keep responses short - "
        "this is a live phone conversation, not a written chat.\n\n"
        f"{_services_summary(garage)}\n"
        f"Opening hours: {_hours_summary(garage)}\n\n"
        "You can look up real, current information and take real actions using your tools - "
        "never invent a price, an appointment time, an opening time, or a booking confirmation. "
        "If a caller asks about something you don't have a tool for, or you cannot confidently "
        "resolve their request after a reasonable attempt, use the request_human_handoff tool "
        "and let them know a member of the team will call them back - never guess, and never "
        "pretend a booking succeeded when it didn't.\n\n"
        "To book an appointment: find out what service they want (use get_appointment_types if "
        "unsure what's offered), find a real available slot (use get_available_slots - never "
        "offer a time you haven't checked), then collect their name, a mobile number to reach "
        "them on, and their vehicle registration, before calling create_booking. Read back what "
        "you're about to book before confirming it. A booking you create is a request pending "
        "the business's own review, not an instant confirmation - say so honestly, e.g. "
        '"I\'ve sent that request through - the team will confirm it with you shortly."'
    )
