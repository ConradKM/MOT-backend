"""The single entry point every channel adapter calls -
``handle_message(garage, channel=..., phone_e164=..., text=..., ...)``.

Owns everything that isn't "what should we actually say/do": idempotency
(Part 36), session load/expiry (Part 35), interrupt-intent detection so a
customer can always ask for a human or bail out mid-flow, turn logging to
the same CommunicationLog timeline the Communications UI already renders
(Part 27), and translating a workflow's result into the session's next
state. All decision-making about what to say lives in workflows.py; all data
access lives in actions.py. This module glues them together and never
touches a booking/customer/vehicle model itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from flask import current_app

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    DIRECTION_SYSTEM,
    CommunicationLog,
)
from app.models.conversation.conversation_session import STATUS_HUMAN_HANDOFF

from . import actions, session_service, workflows
from .intents import (
    ABANDON_FLOW,
    APPOINTMENT_PRICE_QUERY,
    APPOINTMENT_TYPE_QUERY,
    BUSINESS_HOURS_QUERY,
    BUSINESS_LOCATION_QUERY,
    CALLBACK_REQUEST,
    CHECK_APPOINTMENT,
    CHECK_AVAILABILITY,
    CUSTOMER_DETAILS_QUERY,
    GO_BACK,
    GREETING,
    INTERRUPT_INTENTS,
    MOT_EXPIRY_QUERY,
    NAV_INTENTS,
    RESET_FLOW,
    SMALL_TALK,
    SPEAK_TO_HUMAN,
    UNKNOWN,
    RuleBasedIntentResolver,
    detect_faq_intents,
)
from .workflows import ConversationContext, StepResult

logger = logging.getLogger(__name__)

_CHANNEL_MODEL_VALUES = {"VOICE": CHANNEL_VOICE, "WHATSAPP": CHANNEL_WHATSAPP}

# How many consecutive unresolved turns (GENERAL_QUERY/UNKNOWN with nothing
# actionable) before handing off - Part 19's "repeated failed intent
# detection", not a single miss. Overridable per deployment via
# CONVERSATION_MAX_UNRESOLVED_TURNS (raised from a hard 2 so an odd
# transcription or an unknown acronym gets another clear chance first).
_DEFAULT_MAX_UNRESOLVED_TURNS = 3


def _max_unresolved_turns() -> int:
    return int(
        current_app.config.get("CONVERSATION_MAX_UNRESOLVED_TURNS", _DEFAULT_MAX_UNRESOLVED_TURNS)
    )


_resolver = RuleBasedIntentResolver()

_ONE_SHOT_HANDLERS = {
    APPOINTMENT_PRICE_QUERY: workflows.handle_price_query,
    APPOINTMENT_TYPE_QUERY: workflows.handle_appointment_type_query,
    BUSINESS_HOURS_QUERY: workflows.handle_business_hours_query,
    BUSINESS_LOCATION_QUERY: workflows.handle_business_location_query,
    MOT_EXPIRY_QUERY: workflows.handle_mot_expiry_query,
    CUSTOMER_DETAILS_QUERY: workflows.handle_customer_details_query,
    CHECK_APPOINTMENT: workflows.handle_check_appointment,
    SPEAK_TO_HUMAN: workflows.handle_speak_to_human,
    CHECK_AVAILABILITY: workflows.start_booking,
    GREETING: workflows.handle_greeting,
    SMALL_TALK: workflows.handle_small_talk,
    RESET_FLOW: workflows.reset_flow,
    ABANDON_FLOW: workflows.reset_flow,
    GO_BACK: workflows.reset_flow,
}

# The one-shot handlers whose reply is a self-contained FAQ answer - safe to
# run several of in one turn, and to run mid-workflow without touching the
# paused flow.
_FAQ_HANDLERS = {
    APPOINTMENT_PRICE_QUERY: workflows.handle_price_query,
    APPOINTMENT_TYPE_QUERY: workflows.handle_appointment_type_query,
    BUSINESS_HOURS_QUERY: workflows.handle_business_hours_query,
    BUSINESS_LOCATION_QUERY: workflows.handle_business_location_query,
}


@dataclass
class ConversationResult:
    session_id: str
    response_text: str | None
    intent: str
    workflow_step: str | None
    actions_performed: list[str] = field(default_factory=list)
    needs_human: bool = False
    duplicate: bool = False


def handle_message(
    garage,
    *,
    channel: str,
    phone_e164: str,
    text: str,
    external_message_id: str | None = None,
    call_sid: str | None = None,
    now: datetime | None = None,
) -> ConversationResult:
    """Process one inbound customer message end to end. Never raises for an
    ordinary conversational failure - anything it can't handle safely
    becomes a human handoff, not an exception."""
    now = now or datetime.now(UTC)
    text = (text or "").strip()

    customer = actions.find_customer(garage, phone_e164)
    session = session_service.get_or_create_session(
        garage, channel, phone_e164, customer_id=customer.id if customer else None, now=now
    )

    if session_service.already_processed(session, external_message_id):
        return ConversationResult(
            session_id=str(session.id),
            response_text=None,
            intent=session.intent or UNKNOWN,
            workflow_step=session.workflow_step,
            duplicate=True,
        )

    intent_guess = _resolver.resolve(text) if text else UNKNOWN
    _log_turn(
        garage,
        channel,
        phone_e164,
        DIRECTION_INBOUND,
        text,
        customer=customer,
        intent=intent_guess,
        external_id=external_message_id,
        call_sid=call_sid,
    )

    if session.status == STATUS_HUMAN_HANDOFF:
        session_service.touch(session, now=now)
        session_service.mark_processed(session, external_message_id)
        return ConversationResult(
            session_id=str(session.id),
            response_text=None,
            intent=session.intent or UNKNOWN,
            workflow_step=session.workflow_step,
            needs_human=True,
        )

    ctx = ConversationContext(
        garage=garage,
        session=session,
        channel=channel,
        phone_e164=phone_e164,
        customer=customer,
        now=now,
    )
    intent, result = _dispatch(ctx, text, intent_guess)

    _finalize(session, intent, result, now=now)

    if result.response_text:
        _log_turn(
            garage,
            channel,
            phone_e164,
            DIRECTION_OUTBOUND,
            result.response_text,
            customer=customer,
            call_sid=call_sid,
        )
    for description in result.actions_performed:
        _log_turn(
            garage,
            channel,
            phone_e164,
            DIRECTION_SYSTEM,
            description,
            customer=customer,
            call_sid=call_sid,
        )

    session_service.mark_processed(session, external_message_id)

    return ConversationResult(
        session_id=str(session.id),
        response_text=result.response_text,
        intent=session.intent or UNKNOWN,
        workflow_step=session.workflow_step,
        actions_performed=result.actions_performed,
        needs_human=result.needs_human,
    )


def _dispatch(ctx: ConversationContext, text: str, intent_guess: str) -> tuple[str, StepResult]:
    """Route one message. Mid-workflow this classifies the message before
    ever treating it as slot input:

      D) navigation / reset  ("start again", "go back", "cancel that")
      E) human handoff / callback / cancel-an-appointment  (INTERRUPT_INTENTS)
      C) a clear off-topic FAQ ("what time do you close?") - answered, with
         the booking held so it can resume
      A/B) the expected answer, or an in-step correction (the step handler,
           which runs `_maybe_correct` itself)
    """
    session = ctx.session

    if session.workflow_step:
        if intent_guess in NAV_INTENTS:
            return _handle_nav(ctx, text, intent_guess)

        if intent_guess in INTERRUPT_INTENTS:
            return intent_guess, _start_intent(ctx, intent_guess, text)

        aside = _answer_aside(ctx, text)
        if aside is not None:
            return aside

        handler = workflows.STEP_HANDLERS.get(session.workflow_step)
        if handler is not None:
            return session.intent or UNKNOWN, handler(ctx, text)
        logger.warning(
            "[conversation] session %s has unknown workflow_step %r",
            session.id,
            session.workflow_step,
        )

    multi = _answer_multi_faq(ctx, text)
    if multi is not None:
        return multi

    return intent_guess, _start_intent(ctx, intent_guess, text)


def _handle_nav(ctx: ConversationContext, text: str, intent_guess: str) -> tuple[str, StepResult]:
    """Steer-the-conversation commands, handled without the current step ever
    seeing the message (so "back to the beginning" never reaches appointment
    matching)."""
    if intent_guess == RESET_FLOW:
        return RESET_FLOW, workflows.reset_flow(ctx, text)
    if intent_guess == ABANDON_FLOW:
        return ABANDON_FLOW, workflows.abandon_flow(ctx, text)
    # GO_BACK - keep the flow's own intent so the session stays "in" it.
    return ctx.session.intent or GO_BACK, workflows.go_back(ctx, text)


def _combined_faq(ctx: ConversationContext, intents: list[str], text: str) -> str:
    """Run each FAQ handler and join its answer - "prices, hours and where
    are you" comes back as all three, concisely, in one reply."""
    parts: list[str] = []
    for intent in intents:
        handler = _FAQ_HANDLERS.get(intent)
        if handler is None:
            continue
        answer = handler(ctx, text).response_text
        if answer and answer not in parts:
            parts.append(answer)
    return "\n\n".join(parts)


def _answer_multi_faq(ctx: ConversationContext, text: str) -> tuple[str, StepResult] | None:
    """Top-level (no active workflow): a message asking several FAQ things at
    once is answered in full, not narrowed to one."""
    found = detect_faq_intents(text)
    if len(found) < 2:
        return None
    body = _combined_faq(ctx, found, text)
    if not body:
        return None
    return found[0], StepResult(
        response_text=f"{body}\n\nIf you'd like to book one in, just let me know.",
        workflow_step=None,
        complete=True,
    )


def _answer_aside(ctx: ConversationContext, text: str) -> tuple[str, StepResult] | None:
    """Mid-workflow: the message is a clear FAQ, not an answer to the current
    step. Answer it (all of it, if several were asked), then hold the flow at
    AWAITING_RESUME so the customer can carry on."""
    found = detect_faq_intents(text)
    if not found:
        return None
    body = _combined_faq(ctx, found, text)
    if not body:
        return None

    session = ctx.session
    if session.workflow_step == workflows.AWAITING_RESUME:
        paused_step = ctx.slots.get("resume_step")
        paused_intent = ctx.slots.get("resume_intent")
    else:
        paused_step = session.workflow_step
        paused_intent = session.intent

    return session.intent or found[0], StepResult(
        response_text=f"{body}\n\n{workflows.resume_prompt(paused_intent)}",
        workflow_step=workflows.AWAITING_RESUME,
        context_updates={"resume_step": paused_step, "resume_intent": paused_intent},
    )


def _start_intent(ctx: ConversationContext, intent: str, text: str) -> StepResult:
    starter = workflows.INTENT_STARTERS.get(intent)
    if starter is not None:
        return starter(ctx, text)

    one_shot = _ONE_SHOT_HANDLERS.get(intent)
    if one_shot is not None:
        return one_shot(ctx, text)

    if intent == CALLBACK_REQUEST:
        return workflows.start_callback(ctx, text)

    return _unresolved(ctx)


def _unresolved(ctx: ConversationContext) -> StepResult:
    """GENERAL_QUERY / UNKNOWN - clarify (not escalate) for the first few
    turns, then hand off rather than looping forever (Part 19's "repeated
    failed intent detection")."""
    count = int(ctx.slots.get("unresolved_count", 0)) + 1
    if count >= _max_unresolved_turns():
        return StepResult(
            response_text=(
                "I'm not quite following - let me get a member of the team to help you."
            ),
            workflow_step=None,
            needs_human=True,
            handoff_reason="Repeated failed intent detection.",
        )
    if count == 1:
        message = (
            "I can help you book an appointment, check or change an existing one, or answer "
            "questions about prices, opening hours and where we are. What would you like to do?"
        )
    else:
        message = (
            'Sorry, I still didn\'t catch that. Try something like "book an MOT for Friday", '
            '"how much is a service" or "what are your opening hours" - or say "speak to '
            'someone" for the team.'
        )
    return StepResult(
        response_text=message,
        workflow_step=None,
        context_updates={"unresolved_count": count},
    )


def _finalize(session, intent: str, result: StepResult, *, now: datetime) -> None:
    """``result.complete`` (set explicitly by every workflow that has
    genuinely finished - booked, declined, answered, or the session
    otherwise has nothing left to do) is the *only* thing that ends the
    session. ``workflow_step is None`` on its own just means "nothing
    specific pending right now" (e.g. after a one-shot answer, or an
    unresolved turn that still wants its retry counter remembered) - the
    session stays ACTIVE either way, ready for whatever the customer says
    next.

    ``session.intent`` is set here unconditionally, before branching - a
    one-shot query (e.g. a price check) still resolves and reports its real
    intent even though it completes the session immediately afterwards;
    only ``set_workflow_step`` used to record it, so completed one-shot
    turns were silently reported back as UNKNOWN."""
    session.intent = intent
    if getattr(result, "reset_context", False):
        session_service.clear_context(session, now=now)
    if result.needs_human:
        session_service.handoff_to_human(
            session, result.handoff_reason or "Automation could not continue safely.", now=now
        )
    elif result.complete:
        session_service.complete_session(session)
    else:
        session_service.set_workflow_step(
            session, result.workflow_step, intent=intent, now=now, **result.context_updates
        )


def _log_turn(
    garage,
    channel: str,
    phone_e164: str,
    direction: str,
    body: str,
    *,
    customer=None,
    intent=None,
    external_id=None,
    call_sid=None,
) -> None:
    model_channel = _CHANNEL_MODEL_VALUES.get(channel, CHANNEL_WHATSAPP)
    address = f"whatsapp:{phone_e164}" if model_channel == CHANNEL_WHATSAPP else phone_e164
    from_address = address if direction == DIRECTION_INBOUND else None
    to_address = address if direction != DIRECTION_INBOUND else None

    log = CommunicationLog(
        garage_id=garage.id,
        customer_id=customer.id if customer else None,
        channel=model_channel,
        direction=direction,
        external_provider="comaz_conversation_engine",
        external_id=external_id,
        call_sid=call_sid,
        status="received" if direction == DIRECTION_INBOUND else "sent",
        from_address=from_address,
        to_address=to_address,
        body=body,
        intent=intent,
    )
    db.session.add(log)
    db.session.commit()
