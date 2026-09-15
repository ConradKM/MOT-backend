"""The actual multi-turn conversation flows - booking, checking/cancelling/
rescheding an appointment, and the deterministic one-shot queries (price,
hours, location, MOT expiry). engine.py owns session/turn bookkeeping and
logging; everything here is pure decision-making that only ever calls
actions.py for real data/writes.

Every handler returns a :class:`StepResult` - explicit, structured, and
fully sufficient for engine.py to update session state and reply. Nothing
here writes to the session directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from flask import current_app

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.conversation.conversation_session import ConversationSession
from app.models.customer import Customer
from app.models.garage import Garage
from app.models.vehicle import Vehicle

from . import actions
from .appointment_matching import match_appointment_type
from .datetime_parsing import parse_date_phrase, parse_exact_time_phrase, parse_time_window_phrase
from .intents import (
    CANCEL_APPOINTMENT,
    CREATE_BOOKING,
    RESCHEDULE_APPOINTMENT,
    UPDATE_CUSTOMER_NAME,
)

# --- workflow step names ----------------------------------------------------

AWAITING_TYPE = "AWAITING_TYPE"
AWAITING_TYPE_CHOICE = "AWAITING_TYPE_CHOICE"
AWAITING_NAME = "AWAITING_NAME"
AWAITING_DATE = "AWAITING_DATE"
AWAITING_TIME = "AWAITING_TIME"
AWAITING_VEHICLE_CHOICE = "AWAITING_VEHICLE_CHOICE"
AWAITING_VEHICLE_CONFIRM = "AWAITING_VEHICLE_CONFIRM"
AWAITING_VEHICLE_REG = "AWAITING_VEHICLE_REG"
AWAITING_BOOKING_CONFIRMATION = "AWAITING_BOOKING_CONFIRMATION"

AWAITING_CANCEL_CHOICE = "AWAITING_CANCEL_CHOICE"
AWAITING_CANCEL_CONFIRMATION = "AWAITING_CANCEL_CONFIRMATION"

AWAITING_RESCHEDULE_CHOICE = "AWAITING_RESCHEDULE_CHOICE"
AWAITING_RESCHEDULE_DATE = "AWAITING_RESCHEDULE_DATE"
AWAITING_RESCHEDULE_TIME = "AWAITING_RESCHEDULE_TIME"
AWAITING_RESCHEDULE_CONFIRMATION = "AWAITING_RESCHEDULE_CONFIRMATION"

AWAITING_CALLBACK_REASON = "AWAITING_CALLBACK_REASON"

# Not a slot-fill step: the customer asked something off-topic mid-flow, we
# answered it, and now we're waiting to hear whether they want to carry on
# with the paused workflow (context carries `resume_step` / `resume_intent`).
AWAITING_RESUME = "AWAITING_RESUME"

_YES_WORDS = {
    "yes",
    "yep",
    "yeah",
    "yup",
    "ya",
    "y",
    "correct",
    "confirm",
    "confirmed",
    "please",
    "sure",
    "ok",
    "okay",
    "perfect",
    "great",
    "brilliant",
    "lovely",
    "definitely",
    "absolutely",
}
_YES_PHRASES = (
    "go ahead",
    "go for it",
    "please do",
    "yes please",
    "sounds good",
    "sounds great",
    "that works",
    "that's fine",
    "thats fine",
    "that's great",
    "book it",
    "book me in",
    "lets do it",
    "let's do it",
    "do it",
)
_NO_WORDS = {"no", "nope", "not", "cancel", "nah", "n", "dont", "stop"}
_NO_PHRASES = (
    "don't",
    "do not",
    "never mind",
    "nevermind",
    "leave it",
    "not now",
    "not yet",
    "forget it",
    "changed my mind",
    "no thanks",
    "no thank you",
    "cancel that",
)


def _is_yes(text: str) -> bool:
    lowered = text.strip().lower().rstrip(".!")
    return (
        lowered in _YES_WORDS
        or lowered.startswith("yes")
        or any(p in lowered for p in _YES_PHRASES)
    )


def _is_no(text: str) -> bool:
    lowered = text.strip().lower().rstrip(".!")
    return (
        lowered in _NO_WORDS or lowered.startswith("no") or any(p in lowered for p in _NO_PHRASES)
    )


@dataclass
class StepResult:
    response_text: str
    # None => nothing pending (workflow finished, one way or another).
    workflow_step: str | None
    context_updates: dict = field(default_factory=dict)
    actions_performed: list[str] = field(default_factory=list)
    needs_human: bool = False
    handoff_reason: str | None = None
    complete: bool = False
    # Wipe the session's slot context before applying the rest of this
    # result - used by "start again" / "cancel that" so a fresh flow doesn't
    # inherit a half-filled booking. context_updates still merge on top.
    reset_context: bool = False


@dataclass
class ConversationContext:
    garage: Garage
    session: ConversationSession
    channel: str
    phone_e164: str
    customer: Customer | None
    now: datetime

    @property
    def slots(self) -> dict:
        return self.session.context or {}


def _format_date(d: date) -> str:
    return d.strftime("%A %d %B")


def _latest_bookable_day(ctx: ConversationContext) -> date:
    return actions.latest_bookable_day(ctx.garage, now=ctx.now)


def _format_slots(slots: list[dict], limit: int = 4) -> str:
    times = [s["start"] for s in slots if s["status"] != "booked"][:limit]
    return ", ".join(times)


def _max_clarify_rounds() -> int:
    """How many times the booking flow will re-ask "which service?" without a
    confident match before handing to a human - shares the deployment's
    CONVERSATION_MAX_UNRESOLVED_TURNS knob (default 3). An unknown service
    name or acronym gets clarified, never an instant handoff."""
    return int(current_app.config.get("CONVERSATION_MAX_UNRESOLVED_TURNS", 3))


def _get_appointment_type(garage: Garage, type_id: str | None) -> GarageAppointmentType | None:
    if not type_id:
        return None
    for t in garage.appointment_types:
        if str(t.id) == str(type_id) and t.status == "ACTIVE":
            return t
    return None


# --------------------------------------------------------------------------
# CREATE_BOOKING
# --------------------------------------------------------------------------


def start_booking(ctx: ConversationContext, text: str) -> StepResult:
    types = actions.get_appointment_types(ctx.garage)
    if not types:
        return StepResult(
            response_text=(
                "I'm sorry, this business hasn't set up any bookable services yet - "
                "I'll get a member of staff to help you."
            ),
            workflow_step=None,
            needs_human=True,
            handoff_reason="No appointment types configured for this business.",
        )

    match = match_appointment_type(ctx.garage, text)
    if match.matched is not None:
        return _after_type_resolved(ctx, match.matched, text)

    if match.is_ambiguous:
        names = " or ".join(t.name for t in match.candidates)
        return StepResult(
            response_text=f"I can help with that. Did you mean {names}?",
            workflow_step=AWAITING_TYPE_CHOICE,
            context_updates={"candidate_type_ids": [str(t.id) for t in match.candidates]},
        )

    # No confident match. Re-ask which service (an unknown name/acronym is a
    # clarification, not a reason to end the call) - but only up to a
    # configured number of rounds, then hand off *with* a spoken message.
    rounds = int(ctx.slots.get("clarify_rounds", 0))
    if rounds >= _max_clarify_rounds():
        return StepResult(
            response_text=(
                "I'm having trouble matching that to one of our services - "
                "I'll get a member of the team to help you book."
            ),
            workflow_step=None,
            needs_human=True,
            handoff_reason="Could not match a bookable service after repeated clarification.",
        )
    names = ", ".join(t.name for t in types)
    prompt = (
        f"Sure - which of these would you like? {names}"
        if rounds == 0
        else f"Sorry, I didn't catch which service you need. We offer: {names}. Which would you like?"
    )
    return StepResult(
        response_text=prompt,
        workflow_step=AWAITING_TYPE,
        context_updates={"clarify_rounds": rounds + 1},
    )


def _after_type_resolved(
    ctx: ConversationContext, appointment_type: GarageAppointmentType, text: str = ""
) -> StepResult:
    updates = {
        "appointment_type_id": str(appointment_type.id),
        "appointment_type_name": appointment_type.name,
        "clarify_rounds": 0,
    }

    # A date the customer already stated in the opening message ("book an MOT
    # for the 24th September") is honoured - don't ask "what day" again.
    stated_day = parse_date_phrase(text, now=ctx.now)
    if stated_day is not None and stated_day < ctx.now.date():
        stated_day = None

    if ctx.customer is None:
        if stated_day is not None:
            updates["preferred_date"] = stated_day.isoformat()
        return StepResult(
            response_text=f"Great, I can help book a {appointment_type.name}. Could I get your full name first?",
            workflow_step=AWAITING_NAME,
            context_updates=updates,
        )

    if stated_day is not None:
        return _offer_times_for_date(
            ctx,
            stated_day,
            updates,
            time_window=parse_time_window_phrase(text),
            appointment_type=appointment_type,
        )
    return StepResult(
        response_text=f"Great, I can help book a {appointment_type.name}. What day would you like to come in?",
        workflow_step=AWAITING_DATE,
        context_updates=updates,
    )


def handle_awaiting_type(ctx: ConversationContext, text: str) -> StepResult:
    return start_booking(ctx, text)


def handle_awaiting_type_choice(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    candidate_ids = ctx.slots.get("candidate_type_ids", [])
    candidates = [
        t for t in actions.get_appointment_types(ctx.garage) if str(t.id) in candidate_ids
    ]
    match = match_appointment_type(ctx.garage, text)
    if match.matched and str(match.matched.id) in candidate_ids:
        return _after_type_resolved(ctx, match.matched)
    # A direct name mention among exactly the offered candidates.
    lowered = text.lower()
    for t in candidates:
        if t.name.lower() in lowered:
            return _after_type_resolved(ctx, t)

    rounds = int(ctx.slots.get("clarify_rounds", 0))
    if rounds >= _max_clarify_rounds():
        return StepResult(
            response_text=(
                "I'm having trouble matching that to one of our services - "
                "I'll get a member of the team to help you book."
            ),
            workflow_step=None,
            needs_human=True,
            handoff_reason="Could not match a bookable service after repeated clarification.",
        )
    names = " or ".join(t.name for t in candidates)
    return StepResult(
        response_text=f"Sorry, just to confirm - {names}?",
        workflow_step=AWAITING_TYPE_CHOICE,
        context_updates={"clarify_rounds": rounds + 1},
    )


def handle_awaiting_name(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    parts = text.strip().split()
    if len(parts) < 2:
        return StepResult(
            response_text="Sorry, could you give me your first and last name?",
            workflow_step=AWAITING_NAME,
        )
    first, last = parts[0], " ".join(parts[1:])
    name_updates = {"customer_first_name": first, "customer_last_name": last}

    # If they already gave a date in the opening message, go straight to
    # offering times for it rather than asking "what day" now.
    stashed = ctx.slots.get("preferred_date")
    if stashed:
        day = date.fromisoformat(stashed)
        if day >= ctx.now.date():
            return _offer_times_for_date(ctx, day, {**name_updates, "preferred_date": None})

    return StepResult(
        response_text=f"Thanks {first}. What day would you like to come in?",
        workflow_step=AWAITING_DATE,
        context_updates=name_updates,
    )


def _offer_times_for_date(
    ctx: ConversationContext, day: date, updates: dict, time_window=None, appointment_type=None
) -> StepResult:
    if appointment_type is None:
        appointment_type = _get_appointment_type(ctx.garage, ctx.slots.get("appointment_type_id"))
    payload = actions.get_availability_for_day(
        ctx.garage, day, appointment_type=appointment_type, now=ctx.now
    )

    slots = payload["slots"] if payload["is_open"] else []
    if time_window is not None:
        start_bound, end_bound = time_window
        slots = [s for s in slots if start_bound <= time.fromisoformat(s["start"]) <= end_bound]
    slots = [s for s in slots if s["status"] != "booked"]

    if slots:
        updates["preferred_date"] = day.isoformat()
        text_slots = _format_slots(slots)
        return StepResult(
            response_text=f"For {_format_date(day)} I have {text_slots} available. Which would you like?",
            workflow_step=AWAITING_TIME,
            context_updates=updates,
        )

    alt_days = actions.find_next_available_days(
        ctx.garage, appointment_type=appointment_type, start_day=day, now=ctx.now, limit=3
    )
    if alt_days:
        alt_text = ", ".join(_format_date(d) for d in alt_days)
        return StepResult(
            response_text=(
                f"I'm sorry, there's nothing free on {_format_date(day)}. "
                f"The next available days are {alt_text} - would one of those work?"
            ),
            workflow_step=AWAITING_DATE,
            context_updates=updates,
        )

    return StepResult(
        response_text=(
            "I'm sorry, I can't find any availability in the bookable window at the "
            "moment - I'll get a member of staff to help you find a time."
        ),
        workflow_step=None,
        needs_human=True,
        handoff_reason="No availability found for the requested service.",
    )


def handle_awaiting_date(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    day = parse_date_phrase(text, now=ctx.now)
    if day is None:
        return StepResult(
            response_text=(
                "Sorry, what day would you like to come in? You can give me a weekday, "
                '"tomorrow", or a date like "24th September".'
            ),
            workflow_step=AWAITING_DATE,
        )
    if day < ctx.now.date():
        return StepResult(
            response_text="That date's already passed - what day would you like instead?",
            workflow_step=AWAITING_DATE,
        )

    latest = _latest_bookable_day(ctx)
    if day > latest:
        return StepResult(
            response_text=(
                f"We can only take bookings up to {_format_date(latest)} at the moment. "
                "Could you pick a day on or before then?"
            ),
            workflow_step=AWAITING_DATE,
        )

    time_window = parse_time_window_phrase(text)
    return _offer_times_for_date(ctx, day, {}, time_window=time_window)


def handle_awaiting_time(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    day = date.fromisoformat(ctx.slots["preferred_date"])
    exact = parse_exact_time_phrase(text)
    if exact is None:
        window = parse_time_window_phrase(text)
        if window is not None:
            return _offer_times_for_date(ctx, day, {}, time_window=window)
        return StepResult(
            response_text="Sorry, what time would you like (e.g. 10:30)?",
            workflow_step=AWAITING_TIME,
        )

    appointment_type = _get_appointment_type(ctx.garage, ctx.slots.get("appointment_type_id"))
    reason = actions.revalidate_slot(
        ctx.garage, day, exact, appointment_type=appointment_type, now=ctx.now
    )
    if reason is not None:
        return _offer_times_for_date(ctx, day, {}, time_window=None)

    updates = {"preferred_time": exact.isoformat()}
    return _after_time_resolved(ctx, updates)


def _after_time_resolved(ctx: ConversationContext, updates: dict) -> StepResult:
    if ctx.customer is not None:
        vehicles = actions.find_vehicles_for_customer(ctx.customer)
        if len(vehicles) == 1:
            v = vehicles[0]
            updates["vehicle_id"] = str(v.id)
            desc = " ".join(p for p in (v.make, v.model) if p) or "vehicle"
            return StepResult(
                response_text=f"Is this booking for your {desc}, {v.registration_number}?",
                workflow_step=AWAITING_VEHICLE_CONFIRM,
                context_updates=updates,
            )
        if len(vehicles) > 1:
            lines = "\n".join(
                f"{i + 1}. {' '.join(p for p in (v.make, v.model) if p) or 'Vehicle'} - {v.registration_number}"
                for i, v in enumerate(vehicles)
            )
            updates["candidate_vehicle_ids"] = [str(v.id) for v in vehicles]
            return StepResult(
                response_text=f"I have a couple of vehicles for you:\n{lines}\nWhich one is this for?",
                workflow_step=AWAITING_VEHICLE_CHOICE,
                context_updates=updates,
            )

    return StepResult(
        response_text="What's the vehicle's registration number?",
        workflow_step=AWAITING_VEHICLE_REG,
        context_updates=updates,
    )


# --------------------------------------------------------------------------
# Mid-flow corrections / backtracking
#
# A caller can change their mind about the day or the service, or ask to go
# back a step, at any point *before* the BookingRequest is actually created
# (see _maybe_correct's callers). Once the request exists the session is
# COMPLETE and a fresh message goes through RESCHEDULE/CANCEL instead - this
# never mutates a created request.
# --------------------------------------------------------------------------

_GO_BACK_CUES = (
    "go back",
    "step back",
    "back a step",
    "previous step",
    "start over",
    "start again",
)
_CHANGE_DATE_CUES = (
    "another day",
    "a different day",
    "different day",
    "other day",
    "another date",
    "different date",
    "change the date",
    "change the day",
    "change date",
    "wrong day",
    "not that day",
)
_CHANGE_TYPE_CUES = (
    "change the service",
    "different service",
    "change service",
    "wrong service",
    "different appointment",
    "change the appointment type",
)
# Softer phrasing that only counts as a correction when it comes with
# something concrete - a parsable date, or a different appointment type.
_CORRECTION_HINTS = (
    "actually",
    "instead",
    "changed my mind",
    "change my mind",
    "rather",
    "can we do",
    "could we do",
    "what about",
    "how about",
    "make it",
    "let's do",
    "lets do",
)

_POST_DATE_BOOKING_STEPS = frozenset(
    {
        AWAITING_TIME,
        AWAITING_VEHICLE_CONFIRM,
        AWAITING_VEHICLE_CHOICE,
        AWAITING_VEHICLE_REG,
        AWAITING_BOOKING_CONFIRMATION,
    }
)


def _has_cue(text_lower: str, cues) -> bool:
    return any(cue in text_lower for cue in cues)


def _list_types_prompt(ctx: ConversationContext, prefix: str) -> StepResult:
    names = ", ".join(t.name for t in actions.get_appointment_types(ctx.garage))
    return StepResult(
        response_text=f"{prefix} Which service would you like? {names}",
        workflow_step=AWAITING_TYPE,
    )


def _restart_date(ctx: ConversationContext) -> StepResult:
    """Drop any chosen date and time and ask for the day again."""
    return StepResult(
        response_text="Sure - what day would you like to come in instead?",
        workflow_step=AWAITING_DATE,
        context_updates={"preferred_date": None, "preferred_time": None},
    )


def _apply_date_change(ctx: ConversationContext, day: date, text: str) -> StepResult:
    """A new day named mid-flow: clear the previously chosen time and re-offer
    real slots for the new day, via the same authoritative availability path
    the first choice went through."""
    return _offer_times_for_date(
        ctx, day, {"preferred_time": None}, time_window=parse_time_window_phrase(text)
    )


def _apply_type_change(
    ctx: ConversationContext, new_type: GarageAppointmentType, text: str
) -> StepResult:
    """A different service chosen mid-flow. The duration almost always shifts
    which slots fit, so the chosen time is always cleared; if a day was
    already picked, availability is re-queried for it against the new type."""
    updates: dict = {
        "appointment_type_id": str(new_type.id),
        "appointment_type_name": new_type.name,
        "preferred_time": None,
    }
    existing_date = ctx.slots.get("preferred_date")
    if existing_date:
        day = date.fromisoformat(existing_date)
        if day >= ctx.now.date():
            return _offer_times_for_date(
                ctx,
                day,
                updates,
                time_window=parse_time_window_phrase(text),
                appointment_type=new_type,
            )
    updates["preferred_date"] = None
    return StepResult(
        response_text=(
            f"No problem, I've switched that to a {new_type.name}. "
            "What day would you like to come in?"
        ),
        workflow_step=AWAITING_DATE,
        context_updates=updates,
    )


def _step_back(ctx: ConversationContext, step: str | None) -> StepResult:
    """Return to the previous sensible booking step."""
    if step in (AWAITING_NAME, AWAITING_DATE, AWAITING_TYPE_CHOICE):
        return _list_types_prompt(ctx, "No problem, let's start again.")
    if step == AWAITING_TIME:
        return _restart_date(ctx)
    if step in (AWAITING_VEHICLE_CONFIRM, AWAITING_VEHICLE_CHOICE, AWAITING_VEHICLE_REG):
        existing_date = ctx.slots.get("preferred_date")
        if existing_date:
            return _offer_times_for_date(
                ctx,
                date.fromisoformat(existing_date),
                {"preferred_time": None, "vehicle_id": None, "vehicle_registration": None},
            )
        return _restart_date(ctx)
    if step == AWAITING_BOOKING_CONFIRMATION:
        return _after_time_resolved(ctx, {})
    return _list_types_prompt(ctx, "No problem.")


def _maybe_correct(ctx: ConversationContext, text: str) -> StepResult | None:
    """If ``text`` is a correction (new day, new service, or "go back") rather
    than an answer to the question the current step asked, return the
    resulting :class:`StepResult`; otherwise ``None`` so the step's normal
    handler runs. Wired into every booking step from AWAITING_DATE onward,
    before the BookingRequest is created."""
    lowered = text.strip().lower()
    if not lowered:
        return None
    step = ctx.session.workflow_step

    if _has_cue(lowered, _GO_BACK_CUES):
        return _step_back(ctx, step)

    current_type_id = ctx.slots.get("appointment_type_id")
    if current_type_id:
        match = match_appointment_type(ctx.garage, text)
        if (
            match.matched is not None
            and str(match.matched.id) != str(current_type_id)
            and _has_cue(lowered, _CHANGE_TYPE_CUES + _CORRECTION_HINTS)
        ):
            return _apply_type_change(ctx, match.matched, text)

    if step in _POST_DATE_BOOKING_STEPS:
        day = parse_date_phrase(text, now=ctx.now)
        if day is not None and day >= ctx.now.date():
            return _apply_date_change(ctx, day, text)
        if _has_cue(lowered, _CHANGE_DATE_CUES):
            return _restart_date(ctx)

    return None


def handle_awaiting_vehicle_confirm(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    if _is_yes(text):
        return _to_confirmation(ctx, {})
    if _is_no(text):
        return StepResult(
            response_text="No problem - what's the registration number for this booking?",
            workflow_step=AWAITING_VEHICLE_REG,
            context_updates={"vehicle_id": None},
        )
    return StepResult(
        response_text="Sorry, is that the right vehicle - yes or no?",
        workflow_step=AWAITING_VEHICLE_CONFIRM,
    )


def handle_awaiting_vehicle_choice(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    if ctx.customer is None:
        return StepResult(
            response_text="I'll get a member of staff to look into that for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Vehicle choice from unrecognised number.",
        )
    ids = ctx.slots.get("candidate_vehicle_ids", [])
    vehicles = [v for v in actions.find_vehicles_for_customer(ctx.customer) if str(v.id) in ids]
    chosen = _pick_from_list(text, vehicles)
    if chosen is None:
        # Also allow answering with the registration directly.
        chosen = actions.find_customer_vehicle_by_registration(ctx.customer, text)
    if chosen is None:
        return StepResult(
            response_text="Sorry, which number was that - or you can just give me the registration?",
            workflow_step=AWAITING_VEHICLE_CHOICE,
        )
    return _to_confirmation(ctx, {"vehicle_id": str(chosen.id)})


def handle_awaiting_vehicle_reg(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    registration = text.strip().upper().replace(" ", "")
    if not (4 <= len(registration) <= 10):
        return StepResult(
            response_text="Sorry, that doesn't look like a registration number - could you try again?",
            workflow_step=AWAITING_VEHICLE_REG,
        )

    updates: dict = {"vehicle_registration": registration}
    if ctx.customer is not None:
        existing = actions.find_customer_vehicle_by_registration(ctx.customer, registration)
        if existing is not None:
            updates["vehicle_id"] = str(existing.id)
    return _to_confirmation(ctx, updates)


def _pick_from_list[T](text: str, options: list[T]) -> T | None:
    stripped = text.strip()
    if stripped.isdigit():
        idx = int(stripped) - 1
        if 0 <= idx < len(options):
            return options[idx]
    return None


def _to_confirmation(ctx: ConversationContext, updates: dict) -> StepResult:
    slots = dict(ctx.slots)
    slots.update(updates)

    appointment_type = _get_appointment_type(ctx.garage, slots.get("appointment_type_id"))
    day = date.fromisoformat(slots["preferred_date"])
    slot_time = time.fromisoformat(slots["preferred_time"])

    vehicle_desc = slots.get("vehicle_registration")
    if not vehicle_desc and slots.get("vehicle_id") and ctx.customer:
        for v in ctx.customer.vehicles:
            if str(v.id) == slots["vehicle_id"]:
                vehicle_desc = v.registration_number

    price_text = (
        f"£{appointment_type.base_price}"
        if appointment_type and appointment_type.base_price
        else "to be confirmed"
    )
    duration_text = (
        f"Approx. {appointment_type.default_duration_minutes} minutes"
        if appointment_type and appointment_type.default_duration_minutes
        else ""
    )

    summary_lines = [
        appointment_type.name
        if appointment_type
        else slots.get("appointment_type_name", "Appointment"),
        _format_date(day),
        slot_time.strftime("%H:%M"),
        vehicle_desc or "(vehicle not given)",
        price_text,
    ]
    if duration_text:
        summary_lines.append(duration_text)

    return StepResult(
        response_text="Here's what I have:\n"
        + "\n".join(summary_lines)
        + "\n\nShall I go ahead and request this booking?",
        workflow_step=AWAITING_BOOKING_CONFIRMATION,
        context_updates=updates,
    )


def handle_awaiting_booking_confirmation(ctx: ConversationContext, text: str) -> StepResult:
    correction = _maybe_correct(ctx, text)
    if correction is not None:
        return correction

    if not _is_yes(text):
        if _is_no(text):
            return StepResult(
                response_text="No problem, I've not booked anything. Let me know if you'd like to try again.",
                workflow_step=None,
                complete=True,
            )
        return StepResult(
            response_text="Sorry, shall I go ahead and request this booking - yes or no?",
            workflow_step=AWAITING_BOOKING_CONFIRMATION,
        )

    slots = ctx.slots
    appointment_type = _get_appointment_type(ctx.garage, slots.get("appointment_type_id"))
    day = date.fromisoformat(slots["preferred_date"])
    slot_time = time.fromisoformat(slots["preferred_time"])

    first_name = slots.get("customer_first_name") or (
        ctx.customer.first_name if ctx.customer else ""
    )
    last_name = slots.get("customer_last_name") or (ctx.customer.last_name if ctx.customer else "")
    email = ctx.customer.email if ctx.customer else None
    registration = slots.get("vehicle_registration")
    vehicle_make = vehicle_model = None
    if not registration and slots.get("vehicle_id") and ctx.customer:
        for v in ctx.customer.vehicles:
            if str(v.id) == slots["vehicle_id"]:
                registration, vehicle_make, vehicle_model = v.registration_number, v.make, v.model

    booking_request, reason = actions.create_booking_request(
        ctx.garage,
        customer=ctx.customer,
        first_name=first_name,
        last_name=last_name,
        phone_e164=ctx.phone_e164,
        email=email,
        vehicle_registration=registration or "UNKNOWN",
        vehicle_make=vehicle_make,
        vehicle_model=vehicle_model,
        appointment_type=appointment_type,
        preferred_date=day,
        preferred_time=slot_time,
        now=ctx.now,
    )

    if booking_request is None:
        return (
            _offer_times_for_date(ctx, day, {}, time_window=None)
            if reason == "full"
            else StepResult(
                response_text=(
                    "I'm sorry, that time's no longer available - I'll get a member of "
                    "staff to help you find another slot."
                ),
                workflow_step=None,
                needs_human=True,
                handoff_reason=f"Slot became unavailable at confirmation ({reason}).",
            )
        )

    type_label = appointment_type.name if appointment_type else "appointment"
    message = (
        f"You're all set - I've sent your {type_label} request for {_format_date(day)} "
        f"at {slot_time.strftime('%H:%M')} to the team, who'll confirm shortly."
    )
    # On WhatsApp, give the customer their reference so they can check or
    # manage the booking in the portal (see app/customer_auth). Voice keeps
    # the plain wording - a reference code read aloud isn't useful.
    if ctx.channel == "WHATSAPP" and getattr(booking_request, "booking_reference", None):
        message += (
            f"\n\nYour reference is *{booking_request.booking_reference}* - "
            "keep it to check or change this booking."
        )

    return StepResult(
        response_text=message,
        workflow_step=None,
        actions_performed=[f"Booking request #{str(booking_request.id)[:8]} created"],
        complete=True,
    )


BOOKING_STEP_HANDLERS = {
    AWAITING_TYPE: handle_awaiting_type,
    AWAITING_TYPE_CHOICE: handle_awaiting_type_choice,
    AWAITING_NAME: handle_awaiting_name,
    AWAITING_DATE: handle_awaiting_date,
    AWAITING_TIME: handle_awaiting_time,
    AWAITING_VEHICLE_CONFIRM: handle_awaiting_vehicle_confirm,
    AWAITING_VEHICLE_CHOICE: handle_awaiting_vehicle_choice,
    AWAITING_VEHICLE_REG: handle_awaiting_vehicle_reg,
    AWAITING_BOOKING_CONFIRMATION: handle_awaiting_booking_confirmation,
}


# --------------------------------------------------------------------------
# CHECK_APPOINTMENT
# --------------------------------------------------------------------------


def handle_check_appointment(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text=(
                "I don't have your details on file yet - could I get your name so I "
                "can look into that, or would you like a member of staff to call you?"
            ),
            workflow_step=None,
            complete=True,
        )

    upcoming = actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now)
    if not upcoming:
        return StepResult(
            response_text="I can't see any upcoming appointments for you at the moment.",
            workflow_step=None,
            complete=True,
        )
    if len(upcoming) == 1:
        a = upcoming[0]
        type_name = a.appointment_type.name if a.appointment_type else "appointment"
        return StepResult(
            response_text=f"You have a {type_name} booked for {_format_date(a.start_time.date())} at {a.start_time.strftime('%H:%M')}.",
            workflow_step=None,
            complete=True,
        )
    lines = "\n".join(
        f"- {(a.appointment_type.name if a.appointment_type else 'Appointment')} on "
        f"{_format_date(a.start_time.date())} at {a.start_time.strftime('%H:%M')}"
        for a in upcoming
    )
    return StepResult(
        response_text=f"You have {len(upcoming)} upcoming appointments:\n{lines}",
        workflow_step=None,
        complete=True,
    )


# --------------------------------------------------------------------------
# CANCEL_APPOINTMENT
# --------------------------------------------------------------------------


def start_cancel(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I can't find an account for this number - I'll get a member of staff to help you cancel.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Cancellation requested from an unrecognised number.",
        )

    upcoming = actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now)
    if not upcoming:
        return StepResult(
            response_text="I can't see any upcoming appointments for you to cancel.",
            workflow_step=None,
            complete=True,
        )
    if len(upcoming) == 1:
        a = upcoming[0]
        type_name = a.appointment_type.name if a.appointment_type else "appointment"
        return StepResult(
            response_text=(
                f"Just to confirm - cancel your {type_name} on {_format_date(a.start_time.date())} "
                f"at {a.start_time.strftime('%H:%M')}?"
            ),
            workflow_step=AWAITING_CANCEL_CONFIRMATION,
            context_updates={"target_appointment_id": str(a.id)},
        )

    lines = "\n".join(
        f"{i + 1}. {(a.appointment_type.name if a.appointment_type else 'Appointment')} on "
        f"{_format_date(a.start_time.date())} at {a.start_time.strftime('%H:%M')}"
        for i, a in enumerate(upcoming)
    )
    return StepResult(
        response_text=f"Which appointment would you like to cancel?\n{lines}",
        workflow_step=AWAITING_CANCEL_CHOICE,
        context_updates={"candidate_appointment_ids": [str(a.id) for a in upcoming]},
    )


def handle_awaiting_cancel_choice(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I'll get a member of staff to look into that for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Cancel choice from unrecognised number.",
        )
    ids = ctx.slots.get("candidate_appointment_ids", [])
    upcoming = [
        a
        for a in actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now)
        if str(a.id) in ids
    ]
    chosen = _pick_from_list(text, upcoming)
    if chosen is None:
        return StepResult(
            response_text="Sorry, which number was that?",
            workflow_step=AWAITING_CANCEL_CHOICE,
        )
    type_name = chosen.appointment_type.name if chosen.appointment_type else "appointment"
    return StepResult(
        response_text=(
            f"Just to confirm - cancel your {type_name} on {_format_date(chosen.start_time.date())} "
            f"at {chosen.start_time.strftime('%H:%M')}?"
        ),
        workflow_step=AWAITING_CANCEL_CONFIRMATION,
        context_updates={"target_appointment_id": str(chosen.id)},
    )


def handle_awaiting_cancel_confirmation(ctx: ConversationContext, text: str) -> StepResult:
    if not _is_yes(text):
        return StepResult(
            response_text="No problem, I've left your appointment as it is.",
            workflow_step=None,
            complete=True,
        )

    appointment = _find_appointment(ctx, ctx.slots.get("target_appointment_id"))
    if appointment is None:
        return StepResult(
            response_text="Sorry, I couldn't find that appointment any more - I'll get a member of staff to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Target appointment not found at cancel confirmation.",
        )

    ok, reason = actions.cancel_appointment(ctx.garage, appointment)
    if not ok:
        return StepResult(
            response_text="That appointment can't be cancelled any more - I'll get a member of staff to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason=f"Cancel failed: {reason}.",
        )
    return StepResult(
        response_text="Done - your appointment has been cancelled.",
        workflow_step=None,
        actions_performed=[f"Appointment {str(appointment.id)[:8]} cancelled"],
        complete=True,
    )


CANCEL_STEP_HANDLERS = {
    AWAITING_CANCEL_CHOICE: handle_awaiting_cancel_choice,
    AWAITING_CANCEL_CONFIRMATION: handle_awaiting_cancel_confirmation,
}


def _find_appointment(ctx: ConversationContext, appointment_id: str | None):
    if not appointment_id or ctx.customer is None:
        return None
    for a in actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now):
        if str(a.id) == appointment_id:
            return a
    return None


# --------------------------------------------------------------------------
# RESCHEDULE_APPOINTMENT
# --------------------------------------------------------------------------


def start_reschedule(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I can't find an account for this number - I'll get a member of staff to help you reschedule.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Reschedule requested from an unrecognised number.",
        )

    upcoming = actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now)
    if not upcoming:
        return StepResult(
            response_text="I can't see any upcoming appointments for you to move.",
            workflow_step=None,
            complete=True,
        )
    if len(upcoming) == 1:
        a = upcoming[0]
        # Mirrors start_booking's inline appointment-type detection: a
        # customer who already named a day in the same message that
        # triggered the reschedule ("move my appointment to Friday")
        # shouldn't be asked a question they've already answered.
        day = parse_date_phrase(text, now=ctx.now)
        if day is not None and day >= ctx.now.date():
            return _offer_reschedule_slots(ctx, a, day)
        return StepResult(
            response_text="What day would you like to move it to?",
            workflow_step=AWAITING_RESCHEDULE_DATE,
            context_updates={"target_appointment_id": str(a.id)},
        )

    lines = "\n".join(
        f"{i + 1}. {(a.appointment_type.name if a.appointment_type else 'Appointment')} on "
        f"{_format_date(a.start_time.date())} at {a.start_time.strftime('%H:%M')}"
        for i, a in enumerate(upcoming)
    )
    return StepResult(
        response_text=f"Which appointment would you like to move?\n{lines}",
        workflow_step=AWAITING_RESCHEDULE_CHOICE,
        context_updates={"candidate_appointment_ids": [str(a.id) for a in upcoming]},
    )


def handle_awaiting_reschedule_choice(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I'll get a member of staff to look into that for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Reschedule choice from unrecognised number.",
        )
    ids = ctx.slots.get("candidate_appointment_ids", [])
    upcoming = [
        a
        for a in actions.get_upcoming_appointments(ctx.garage, ctx.customer, now=ctx.now)
        if str(a.id) in ids
    ]
    chosen = _pick_from_list(text, upcoming)
    if chosen is None:
        return StepResult(
            response_text="Sorry, which number was that?", workflow_step=AWAITING_RESCHEDULE_CHOICE
        )
    return StepResult(
        response_text="What day would you like to move it to?",
        workflow_step=AWAITING_RESCHEDULE_DATE,
        context_updates={"target_appointment_id": str(chosen.id)},
    )


def handle_awaiting_reschedule_date(ctx: ConversationContext, text: str) -> StepResult:
    appointment = _find_appointment(ctx, ctx.slots.get("target_appointment_id"))
    if appointment is None:
        return StepResult(
            response_text="Sorry, I've lost track of which appointment - I'll get a member of staff to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Target appointment not found during reschedule.",
        )

    day = parse_date_phrase(text, now=ctx.now)
    if day is None or day < ctx.now.date():
        return StepResult(
            response_text="Sorry, what day would you like instead?",
            workflow_step=AWAITING_RESCHEDULE_DATE,
        )

    return _offer_reschedule_slots(ctx, appointment, day)


def _offer_reschedule_slots(ctx: ConversationContext, appointment, day: date) -> StepResult:
    """Real availability for `day`, for the appointment's own type - shared
    by start_reschedule (date named in the same message) and
    handle_awaiting_reschedule_date (date given as a follow-up), so both
    paths check the same real slots before ever proposing a time."""
    payload = actions.get_availability_for_day(
        ctx.garage, day, appointment_type=appointment.appointment_type, now=ctx.now
    )
    slots = [s for s in payload["slots"] if s["status"] != "booked"] if payload["is_open"] else []
    if not slots:
        return StepResult(
            response_text=f"There's nothing free on {_format_date(day)} - could you try another day?",
            workflow_step=AWAITING_RESCHEDULE_DATE,
            context_updates={"target_appointment_id": str(appointment.id)},
        )
    return StepResult(
        response_text=f"For {_format_date(day)} I have {_format_slots(slots)} available. Which would you like?",
        workflow_step=AWAITING_RESCHEDULE_TIME,
        context_updates={"target_appointment_id": str(appointment.id), "new_date": day.isoformat()},
    )


def handle_awaiting_reschedule_time(ctx: ConversationContext, text: str) -> StepResult:
    appointment = _find_appointment(ctx, ctx.slots.get("target_appointment_id"))
    if appointment is None:
        return StepResult(
            response_text="Sorry, I've lost track of which appointment - I'll get a member of staff to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Target appointment not found during reschedule.",
        )
    day = date.fromisoformat(ctx.slots["new_date"])
    exact = parse_exact_time_phrase(text)
    if exact is None:
        return StepResult(
            response_text="Sorry, what time would you like (e.g. 10:30)?",
            workflow_step=AWAITING_RESCHEDULE_TIME,
        )

    return StepResult(
        response_text=(
            f"Just to confirm - move your appointment to {_format_date(day)} at {exact.strftime('%H:%M')}?"
        ),
        workflow_step=AWAITING_RESCHEDULE_CONFIRMATION,
        context_updates={"new_time": exact.isoformat()},
    )


def handle_awaiting_reschedule_confirmation(ctx: ConversationContext, text: str) -> StepResult:
    if not _is_yes(text):
        return StepResult(
            response_text="No problem, I've left your appointment as it is.",
            workflow_step=None,
            complete=True,
        )

    appointment = _find_appointment(ctx, ctx.slots.get("target_appointment_id"))
    if appointment is None:
        return StepResult(
            response_text="Sorry, I've lost track of which appointment - I'll get a member of staff to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Target appointment not found at reschedule confirmation.",
        )
    day = date.fromisoformat(ctx.slots["new_date"])
    new_time = time.fromisoformat(ctx.slots["new_time"])

    ok, reason = actions.reschedule_appointment(ctx.garage, appointment, day, new_time)
    if not ok:
        return StepResult(
            response_text=(
                "I'm sorry, that time's no longer available - I'll get a member of "
                "staff to help you find another."
            ),
            workflow_step=None,
            needs_human=True,
            handoff_reason=f"Reschedule failed: {reason}.",
        )
    return StepResult(
        response_text=f"Done - your appointment is now on {_format_date(day)} at {new_time.strftime('%H:%M')}.",
        workflow_step=None,
        actions_performed=[f"Appointment {str(appointment.id)[:8]} rescheduled"],
        complete=True,
    )


RESCHEDULE_STEP_HANDLERS = {
    AWAITING_RESCHEDULE_CHOICE: handle_awaiting_reschedule_choice,
    AWAITING_RESCHEDULE_DATE: handle_awaiting_reschedule_date,
    AWAITING_RESCHEDULE_TIME: handle_awaiting_reschedule_time,
    AWAITING_RESCHEDULE_CONFIRMATION: handle_awaiting_reschedule_confirmation,
}


# --------------------------------------------------------------------------
# UPDATE_CUSTOMER_NAME  - correct the customer's own name on their CoMaz
# record. Verification here is possession of the WhatsApp number the record
# is matched to (ctx.customer is set only when the inbound number already
# belongs to a known customer). The write goes through actions.py to the
# canonical Customer row, so the fix shows everywhere in CoMaz, not just in
# this session. Email / phone / address changes are deliberately NOT done
# here (see handle_update_contact_details).
# --------------------------------------------------------------------------

AWAITING_NAME_CORRECTION = "AWAITING_NAME_CORRECTION"
AWAITING_NAME_CORRECTION_CONFIRM = "AWAITING_NAME_CORRECTION_CONFIRM"

# Cues that introduce the corrected name, e.g. "... it should be Jon Reid".
# The *rightmost* match wins, so "you've spelt my name wrong, it should be
# Jon Reid" takes the tail after "should be", not after "spelt".
_NAME_CUE = re.compile(
    r"\b(?:should\s+be|chang(?:e|ed)\s+(?:it\s+)?to|correct(?:ed)?\s+(?:it\s+)?to|"
    r"make\s+it|put\s+it\s+(?:down\s+)?as|spell\s+it|name\s+is|name\s+to|name\s*[:=]|it'?s|its)\b",
    re.IGNORECASE,
)
_NAME_STOP_WORDS = {
    "wrong", "not", "the", "a", "my", "name", "is", "please", "actually",
    "no", "yes", "it", "should", "be", "change", "correct", "to",
}  # fmt: skip
_NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'\-.]*")


def _extract_name(text: str, *, whole_is_name: bool) -> tuple[str, str] | None:
    """A first + last name from the message. When ``whole_is_name`` the entire
    reply is treated as the name ("Jon Reid" answered to "what should it
    be?"); otherwise a name is only taken when a cue introduces it ("... it
    should be Jon Reid"), so "can you change my name?" yields nothing and we
    ask. Returns ``None`` when there's no clean two-part name."""
    stripped = text.strip()
    matches = list(_NAME_CUE.finditer(stripped))
    if matches:
        candidate = stripped[matches[-1].end() :]
    elif whole_is_name:
        candidate = stripped
    else:
        return None

    candidate = candidate.strip(" .!?,\"':;-").strip()
    candidate = re.sub(r"^(my\s+name\s+)", "", candidate, flags=re.IGNORECASE).strip()
    candidate = re.sub(r"\s+(please|thanks|thank\s+you|ta)$", "", candidate, flags=re.IGNORECASE)

    parts = [p for p in candidate.split() if p]
    if not (2 <= len(parts) <= 4):
        return None
    if any(p.lower() in _NAME_STOP_WORDS for p in parts):
        return None
    if not all(_NAME_WORD.fullmatch(p) for p in parts):
        return None
    return parts[0].strip("."), " ".join(parts[1:])


def _need_identity_for_change(ctx: ConversationContext, what: str) -> StepResult:
    return StepResult(
        response_text=(
            f"I can only {what} once I can confirm who I'm speaking to, and this "
            "number isn't linked to an account yet. I'll get a member of the team to help."
        ),
        workflow_step=None,
        needs_human=True,
        handoff_reason=f"{what} requested from an unrecognised number.",
    )


def start_update_name(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return _need_identity_for_change(ctx, "update the name on an account")

    current = f"{ctx.customer.first_name} {ctx.customer.last_name}"
    name = _extract_name(text, whole_is_name=False)
    if name is None:
        return StepResult(
            response_text=(
                f"I've got your name as {current}. What should it be? Please send your full name."
            ),
            workflow_step=AWAITING_NAME_CORRECTION,
        )
    return StepResult(
        response_text=(
            f"Just to confirm - change the name on your account from {current} to "
            f"{name[0]} {name[1]}?"
        ),
        workflow_step=AWAITING_NAME_CORRECTION_CONFIRM,
        context_updates={"pending_first_name": name[0], "pending_last_name": name[1]},
    )


def handle_awaiting_name_correction(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return _need_identity_for_change(ctx, "update the name on an account")
    name = _extract_name(text, whole_is_name=True)
    if name is None:
        return StepResult(
            response_text="Sorry, could you send your full name (first and last)?",
            workflow_step=AWAITING_NAME_CORRECTION,
        )
    current = f"{ctx.customer.first_name} {ctx.customer.last_name}"
    return StepResult(
        response_text=(
            f"Just to confirm - change the name on your account from {current} to "
            f"{name[0]} {name[1]}?"
        ),
        workflow_step=AWAITING_NAME_CORRECTION_CONFIRM,
        context_updates={"pending_first_name": name[0], "pending_last_name": name[1]},
    )


def handle_awaiting_name_correction_confirm(ctx: ConversationContext, text: str) -> StepResult:
    first = ctx.slots.get("pending_first_name")
    last = ctx.slots.get("pending_last_name")
    if not first or not last:
        return start_update_name(ctx, "")
    if ctx.customer is None:
        return _need_identity_for_change(ctx, "update the name on an account")

    if _is_no(text):
        return StepResult(
            response_text=(
                f"No problem - I've left it as {ctx.customer.first_name} {ctx.customer.last_name}."
            ),
            workflow_step=None,
            complete=True,
            reset_context=True,
        )
    if not _is_yes(text):
        return StepResult(
            response_text=f"Shall I change your name to {first} {last}? (yes/no)",
            workflow_step=AWAITING_NAME_CORRECTION_CONFIRM,
        )

    ok, reason = actions.update_customer_name(ctx.garage, ctx.customer, first, last)
    if not ok:
        return StepResult(
            response_text="I couldn't update that - I'll get a member of the team to sort it.",
            workflow_step=None,
            needs_human=True,
            handoff_reason=f"Customer name update failed ({reason}).",
        )
    return StepResult(
        response_text=f"Done - your name is now {first} {last}. That'll update across our system.",
        workflow_step=None,
        complete=True,
        reset_context=True,
        actions_performed=[f"Customer {str(ctx.customer.id)[:8]} name updated to {first} {last}"],
    )


def handle_update_contact_details(ctx: ConversationContext, text: str) -> StepResult:
    """Any other "change my email/phone/address" - not something WhatsApp can
    safely do. Explain plainly and hand to a human; never loop back into
    booking."""
    return StepResult(
        response_text=(
            "For security I can't change contact details like your email, phone number "
            "or address over WhatsApp. I'll pass this to the team and they'll sort it "
            "for you. Is there anything else I can help with in the meantime?"
        ),
        workflow_step=None,
        needs_human=True,
        handoff_reason="Customer asked to change contact details (not permitted over WhatsApp).",
    )


NAME_UPDATE_STEP_HANDLERS = {
    AWAITING_NAME_CORRECTION: handle_awaiting_name_correction,
    AWAITING_NAME_CORRECTION_CONFIRM: handle_awaiting_name_correction_confirm,
}


# --------------------------------------------------------------------------
# One-shot deterministic queries
# --------------------------------------------------------------------------


def handle_price_query(ctx: ConversationContext, text: str) -> StepResult:
    match = match_appointment_type(ctx.garage, text)
    if match.matched is not None:
        t = match.matched
        if t.base_price is not None:
            return StepResult(
                response_text=f"{t.name} is £{t.base_price}.", workflow_step=None, complete=True
            )
        return StepResult(
            response_text=f"Pricing for {t.name} is confirmed by the business - I'll get someone to call you back with a price.",
            workflow_step=None,
            complete=True,
        )
    if match.is_ambiguous:
        names = " or ".join(t.name for t in match.candidates)
        return StepResult(response_text=f"Did you mean {names}?", workflow_step=None, complete=True)

    types = actions.get_appointment_types(ctx.garage)
    names = ", ".join(f"{t.name} (£{t.base_price})" if t.base_price else t.name for t in types)
    return StepResult(
        response_text=f"Here's what we offer: {names}", workflow_step=None, complete=True
    )


def handle_appointment_type_query(ctx: ConversationContext, text: str) -> StepResult:
    types = actions.get_appointment_types(ctx.garage)
    if not types:
        return StepResult(
            response_text="I'll get a member of staff to tell you what's available.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="No appointment types configured.",
        )
    names = ", ".join(t.name for t in types)
    return StepResult(
        response_text=f"We offer: {names}. Would you like to book one?",
        workflow_step=None,
        complete=True,
    )


_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def handle_business_hours_query(ctx: ConversationContext, text: str) -> StepResult:
    hours = actions.get_business_hours(ctx.garage)
    lines = []
    for wd in range(7):
        opens_at, closes_at, is_closed = hours[wd]
        if is_closed:
            lines.append(f"{_WEEKDAY_NAMES[wd]}: closed")
        else:
            lines.append(
                f"{_WEEKDAY_NAMES[wd]}: {opens_at.strftime('%H:%M')}-{closes_at.strftime('%H:%M')}"
            )
    return StepResult(
        response_text="Our opening hours are:\n" + "\n".join(lines),
        workflow_step=None,
        complete=True,
    )


def handle_business_location_query(ctx: ConversationContext, text: str) -> StepResult:
    bits = [b for b in (ctx.garage.address, ctx.garage.postcode) if b]
    if not bits:
        return StepResult(
            response_text="I don't have an address on file for this business yet - I'll get someone to help.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="No business address configured.",
        )
    return StepResult(
        response_text=f"We're at {', '.join(bits)}.", workflow_step=None, complete=True
    )


def handle_mot_expiry_query(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I don't have your vehicle on file yet - what's the registration number?",
            workflow_step=AWAITING_VEHICLE_REG_FOR_MOT,
        )
    vehicles = actions.find_vehicles_for_customer(ctx.customer)
    if not vehicles:
        return StepResult(
            response_text="I don't have a vehicle on file for you yet - what's the registration number?",
            workflow_step=AWAITING_VEHICLE_REG_FOR_MOT,
        )
    if len(vehicles) > 1:
        lines = "\n".join(f"{i + 1}. {v.registration_number}" for i, v in enumerate(vehicles))
        return StepResult(
            response_text=f"Which vehicle?\n{lines}",
            workflow_step=AWAITING_MOT_VEHICLE_CHOICE,
            context_updates={"candidate_vehicle_ids": [str(v.id) for v in vehicles]},
        )
    return _mot_expiry_reply(vehicles[0])


AWAITING_VEHICLE_REG_FOR_MOT = "AWAITING_VEHICLE_REG_FOR_MOT"
AWAITING_MOT_VEHICLE_CHOICE = "AWAITING_MOT_VEHICLE_CHOICE"


def _mot_expiry_reply(vehicle: Vehicle) -> StepResult:
    expiry = actions.get_mot_expiry(vehicle)
    if expiry is None:
        return StepResult(
            response_text=f"I don't have an MOT expiry date on file for {vehicle.registration_number}.",
            workflow_step=None,
            complete=True,
        )
    return StepResult(
        response_text=f"The MOT for {vehicle.registration_number} expires on {expiry.strftime('%d %B %Y')}.",
        workflow_step=None,
        complete=True,
    )


def handle_awaiting_vehicle_reg_for_mot(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I'll get a member of staff to look into that for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="MOT query from unrecognised number.",
        )
    vehicle = actions.find_customer_vehicle_by_registration(ctx.customer, text)
    if vehicle is None:
        return StepResult(
            response_text="I can't find that registration on your account - I'll get someone to check for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Vehicle not found for MOT query.",
        )
    return _mot_expiry_reply(vehicle)


def handle_awaiting_mot_vehicle_choice(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I'll get a member of staff to look into that for you.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="MOT vehicle choice from unrecognised number.",
        )
    ids = ctx.slots.get("candidate_vehicle_ids", [])
    vehicles = [v for v in actions.find_vehicles_for_customer(ctx.customer) if str(v.id) in ids]
    chosen = _pick_from_list(text, vehicles) or actions.find_customer_vehicle_by_registration(
        ctx.customer, text
    )
    if chosen is None:
        return StepResult(
            response_text="Sorry, which number was that?", workflow_step=AWAITING_MOT_VEHICLE_CHOICE
        )
    return _mot_expiry_reply(chosen)


MOT_STEP_HANDLERS = {
    AWAITING_VEHICLE_REG_FOR_MOT: handle_awaiting_vehicle_reg_for_mot,
    AWAITING_MOT_VEHICLE_CHOICE: handle_awaiting_mot_vehicle_choice,
}


def handle_customer_details_query(ctx: ConversationContext, text: str) -> StepResult:
    if ctx.customer is None:
        return StepResult(
            response_text="I don't have an account on file for this number yet.",
            workflow_step=None,
            complete=True,
        )
    vehicles = actions.find_vehicles_for_customer(ctx.customer)
    if vehicles:
        reg_list = ", ".join(v.registration_number for v in vehicles)
        return StepResult(
            response_text=f"Hi {ctx.customer.first_name}, I have you down with: {reg_list}.",
            workflow_step=None,
            complete=True,
        )
    return StepResult(
        response_text=f"Hi {ctx.customer.first_name}, I don't have any vehicles on file for you yet.",
        workflow_step=None,
        complete=True,
    )


# --------------------------------------------------------------------------
# GREETING / SMALL_TALK - a message that's only a hello or a thanks
# --------------------------------------------------------------------------


def handle_greeting(ctx: ConversationContext, text: str) -> StepResult:
    """A bare "hi" / "help" / "what can you do" - a warm capability intro
    rather than "sorry, didn't follow that". WhatsApp gets a short bulleted
    menu; Voice gets the same in one spoken sentence."""
    name = f"{ctx.customer.first_name}, " if ctx.customer else ""
    types = actions.get_appointment_types(ctx.garage)
    services = ", ".join(t.name for t in types[:4]) if types else ""

    if ctx.channel == "WHATSAPP":
        lines = [
            f"Hi {name}you've reached {ctx.garage.name}. I can help you:",
            f"- book an appointment{f' ({services})' if services else ''}",
            "- check, change or cancel an existing booking",
            "- answer questions about prices, opening hours or where we are",
            "",
            "What would you like to do?",
        ]
        response = "\n".join(lines)
    else:
        response = (
            f"Hi {name}you've reached {ctx.garage.name}. I can help you book, check, change "
            "or cancel an appointment, or answer questions about prices and opening hours. "
            "What would you like to do?"
        )
    return StepResult(response_text=response, workflow_step=None, complete=True)


def handle_small_talk(ctx: ConversationContext, text: str) -> StepResult:
    return StepResult(
        response_text=(
            "You're welcome! Message here any time you need to book or change an appointment."
        ),
        workflow_step=None,
        complete=True,
    )


# --------------------------------------------------------------------------
# Flow control - "start again" / "cancel that" / "go back" / resume-after-aside
# --------------------------------------------------------------------------

_MENU_LINE = (
    "I can help you book, check, change or cancel an appointment, or answer "
    "questions about prices, opening hours and where we are."
)


def reset_flow(ctx: ConversationContext, text: str = "") -> StepResult:
    """ "Start again" / "back to the beginning" - drop any half-filled booking
    and hand control back to the customer."""
    return StepResult(
        response_text=f"No problem - let's start fresh. What would you like to do? {_MENU_LINE}",
        workflow_step=None,
        reset_context=True,
    )


def abandon_flow(ctx: ConversationContext, text: str = "") -> StepResult:
    """ "Cancel that" / "never mind" - stop the current flow. Nothing was
    booked; the next message starts clean."""
    return StepResult(
        response_text=(
            "Okay, I've stopped that - nothing has been booked. "
            "Message any time you'd like to pick it back up."
        ),
        workflow_step=None,
        complete=True,
        reset_context=True,
    )


def go_back(ctx: ConversationContext, text: str = "") -> StepResult:
    """ "Go back a step". Booking has a real step-by-step history to walk;
    the shorter cancel/reschedule flows just restart cleanly."""
    step = ctx.session.workflow_step
    if ctx.session.intent == CREATE_BOOKING and step in BOOKING_STEP_HANDLERS:
        return _step_back(ctx, step)
    if step == AWAITING_RESUME:
        return _reprompt_step(ctx, ctx.slots.get("resume_step"), ctx.slots.get("resume_intent"))
    return reset_flow(ctx)


_RESUME_VERBS = {
    CREATE_BOOKING: "carry on with your booking",
    RESCHEDULE_APPOINTMENT: "carry on rescheduling your appointment",
    CANCEL_APPOINTMENT: "carry on cancelling your appointment",
}
_RESUME_YES = (
    "continue",
    "carry on",
    "carry-on",
    "keep going",
    "keep booking",
    "go on",
    "resume",
    "back to booking",
    "back to the booking",
    "the booking",
    "my booking",
    "where we were",
    "where we left off",
)


def resume_prompt(intent: str | None) -> str:
    verb = _RESUME_VERBS.get(intent or "", "carry on where we left off")
    return f"Would you like to {verb}?"


def _reprompt_step(
    ctx: ConversationContext, step: str | None, intent: str | None = None
) -> StepResult:
    """Re-ask the question a paused workflow step was waiting on, so a
    resumed conversation doesn't leave the customer guessing."""
    slots = ctx.slots
    if step == AWAITING_TYPE or step == AWAITING_TYPE_CHOICE:
        names = ", ".join(t.name for t in actions.get_appointment_types(ctx.garage))
        return StepResult(
            response_text=f"Great - which service would you like? {names}",
            workflow_step=AWAITING_TYPE,
        )
    if step == AWAITING_NAME:
        return StepResult(
            response_text="Could I get your full name to put on the booking?",
            workflow_step=AWAITING_NAME,
        )
    if step == AWAITING_DATE:
        return StepResult(
            response_text="What day would you like to come in?", workflow_step=AWAITING_DATE
        )
    if step == AWAITING_TIME and slots.get("preferred_date"):
        return _offer_times_for_date(ctx, date.fromisoformat(slots["preferred_date"]), {})
    if step in (AWAITING_VEHICLE_CONFIRM, AWAITING_VEHICLE_CHOICE) and slots.get("preferred_time"):
        return _after_time_resolved(ctx, {})
    if step == AWAITING_VEHICLE_REG:
        return StepResult(
            response_text="What's the vehicle's registration number?",
            workflow_step=AWAITING_VEHICLE_REG,
        )
    if step == AWAITING_BOOKING_CONFIRMATION and slots.get("preferred_time"):
        return _to_confirmation(ctx, {})
    if step in (AWAITING_RESCHEDULE_DATE, AWAITING_RESCHEDULE_TIME):
        return StepResult(
            response_text="What day would you like to move it to?",
            workflow_step=AWAITING_RESCHEDULE_DATE,
        )
    # Anything we can't cleanly re-enter (a mid-vehicle step with no time, an
    # unknown step): fall back to a clean start rather than a dead end.
    return reset_flow(ctx)


def handle_awaiting_resume(ctx: ConversationContext, text: str) -> StepResult:
    """After we answered an off-topic question mid-flow: is the customer
    carrying on, dropping it, or just going ahead and answering the paused
    step? (Another off-topic question is caught by engine._dispatch before
    it reaches here.)"""
    lowered = text.strip().lower()
    resume_step = ctx.slots.get("resume_step")
    resume_intent = ctx.slots.get("resume_intent")

    if _is_no(text):
        return StepResult(
            response_text=(
                "No problem - I haven't booked anything. Message any time you'd like to."
            ),
            workflow_step=None,
            complete=True,
            reset_context=True,
        )

    handler = STEP_HANDLERS.get(resume_step) if resume_step else None
    if handler is None:
        return StepResult(
            response_text=f"Let's carry on. What would you like to do? {_MENU_LINE}",
            workflow_step=None,
        )

    if _is_yes(text) or any(p in lowered for p in _RESUME_YES):
        return _reprompt_step(ctx, resume_step, resume_intent)

    # Not an explicit yes/no - treat it as the answer to the paused step, so
    # "Friday" both resumes and moves the booking on.
    return handler(ctx, text)


# --------------------------------------------------------------------------
# SPEAK_TO_HUMAN / CALLBACK_REQUEST
# --------------------------------------------------------------------------


def handle_speak_to_human(ctx: ConversationContext, text: str) -> StepResult:
    return StepResult(
        response_text="Of course - I'll get a member of the team to pick this up.",
        workflow_step=None,
        needs_human=True,
        handoff_reason="Customer asked to speak to a person.",
    )


def start_callback(ctx: ConversationContext, text: str) -> StepResult:
    return StepResult(
        response_text="No problem - is there anything I should let them know before they call?",
        workflow_step=AWAITING_CALLBACK_REASON,
    )


def handle_awaiting_callback_reason(ctx: ConversationContext, text: str) -> StepResult:
    reason = None if _is_no(text) else text.strip()
    callback = actions.create_callback_request(
        ctx.garage,
        customer=ctx.customer,
        phone_e164=ctx.phone_e164,
        reason=reason,
        session=ctx.session,
    )
    return StepResult(
        response_text="Thanks - someone will call you back shortly.",
        workflow_step=None,
        actions_performed=[f"Callback request #{str(callback.id)[:8]} created"],
        complete=True,
    )


CALLBACK_STEP_HANDLERS = {
    AWAITING_CALLBACK_REASON: handle_awaiting_callback_reason,
}


# --------------------------------------------------------------------------
# Combined step dispatch - engine.py only needs this one table.
# --------------------------------------------------------------------------

STEP_HANDLERS = {
    **BOOKING_STEP_HANDLERS,
    **CANCEL_STEP_HANDLERS,
    **RESCHEDULE_STEP_HANDLERS,
    **NAME_UPDATE_STEP_HANDLERS,
    **MOT_STEP_HANDLERS,
    **CALLBACK_STEP_HANDLERS,
    AWAITING_RESUME: handle_awaiting_resume,
}

INTENT_STARTERS = {
    CREATE_BOOKING: start_booking,
    CANCEL_APPOINTMENT: start_cancel,
    RESCHEDULE_APPOINTMENT: start_reschedule,
    UPDATE_CUSTOMER_NAME: start_update_name,
}
