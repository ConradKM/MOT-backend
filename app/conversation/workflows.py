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

from dataclasses import dataclass, field
from datetime import date, datetime, time

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.conversation.conversation_session import ConversationSession
from app.models.customer import Customer
from app.models.garage import Garage
from app.models.vehicle import Vehicle

from . import actions
from .appointment_matching import match_appointment_type
from .datetime_parsing import parse_date_phrase, parse_exact_time_phrase, parse_time_window_phrase
from .intents import CANCEL_APPOINTMENT, CREATE_BOOKING, RESCHEDULE_APPOINTMENT

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

_YES_WORDS = {
    "yes",
    "yep",
    "yeah",
    "yup",
    "correct",
    "confirm",
    "confirmed",
    "please",
    "sure",
    "ok",
    "okay",
}
_NO_WORDS = {"no", "nope", "not", "cancel", "nah"}


def _is_yes(text: str) -> bool:
    lowered = text.strip().lower().rstrip(".!")
    return lowered in _YES_WORDS or lowered.startswith("yes")


def _is_no(text: str) -> bool:
    lowered = text.strip().lower().rstrip(".!")
    return lowered in _NO_WORDS or lowered.startswith("no")


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


def _format_slots(slots: list[dict], limit: int = 4) -> str:
    times = [s["start"] for s in slots if s["status"] != "booked"][:limit]
    return ", ".join(times)


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
        return _after_type_resolved(ctx, match.matched)

    if match.is_ambiguous:
        names = " or ".join(t.name for t in match.candidates)
        return StepResult(
            response_text=f"I can help with that. Did you mean {names}?",
            workflow_step=AWAITING_TYPE_CHOICE,
            context_updates={"candidate_type_ids": [str(t.id) for t in match.candidates]},
        )

    names = ", ".join(t.name for t in types)
    return StepResult(
        response_text=f"Sure - which of these would you like? {names}",
        workflow_step=AWAITING_TYPE,
    )


def _after_type_resolved(
    ctx: ConversationContext, appointment_type: GarageAppointmentType
) -> StepResult:
    updates = {
        "appointment_type_id": str(appointment_type.id),
        "appointment_type_name": appointment_type.name,
    }

    if ctx.customer is None:
        return StepResult(
            response_text=f"Great, I can help book a {appointment_type.name}. Could I get your full name first?",
            workflow_step=AWAITING_NAME,
            context_updates=updates,
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

    names = " or ".join(t.name for t in candidates)
    return StepResult(
        response_text=f"Sorry, just to confirm - {names}?",
        workflow_step=AWAITING_TYPE_CHOICE,
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
    return StepResult(
        response_text=f"Thanks {first}. What day would you like to come in?",
        workflow_step=AWAITING_DATE,
        context_updates={"customer_first_name": first, "customer_last_name": last},
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
            response_text="Sorry, what day would you like to come in (e.g. Tuesday, or tomorrow)?",
            workflow_step=AWAITING_DATE,
        )
    if day < ctx.now.date():
        return StepResult(
            response_text="That date's already passed - what day would you like instead?",
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

    return StepResult(
        response_text=(
            f"You're all set - I've sent your {appointment_type.name if appointment_type else 'appointment'} "
            f"request for {_format_date(day)} at {slot_time.strftime('%H:%M')} to the team, who'll confirm "
            "shortly."
        ),
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
    **MOT_STEP_HANDLERS,
    **CALLBACK_STEP_HANDLERS,
}

INTENT_STARTERS = {
    CREATE_BOOKING: start_booking,
    CANCEL_APPOINTMENT: start_cancel,
    RESCHEDULE_APPOINTMENT: start_reschedule,
}
