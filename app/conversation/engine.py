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
    APPOINTMENT_PRICE_QUERY,
    APPOINTMENT_TYPE_QUERY,
    BUSINESS_HOURS_QUERY,
    BUSINESS_LOCATION_QUERY,
    CALLBACK_REQUEST,
    CHECK_APPOINTMENT,
    CHECK_AVAILABILITY,
    CUSTOMER_DETAILS_QUERY,
    INTERRUPT_INTENTS,
    MOT_EXPIRY_QUERY,
    SPEAK_TO_HUMAN,
    UNKNOWN,
    RuleBasedIntentResolver,
)
from .workflows import ConversationContext, StepResult

logger = logging.getLogger(__name__)

_CHANNEL_MODEL_VALUES = {"VOICE": CHANNEL_VOICE, "WHATSAPP": CHANNEL_WHATSAPP}

# How many consecutive unresolved turns (GENERAL_QUERY/UNKNOWN with nothing
# actionable) before handing off - Part 19's "repeated failed intent
# detection", not a single miss.
_MAX_UNRESOLVED_TURNS = 2

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
    session = ctx.session

    if session.workflow_step:
        # A customer can always interrupt an in-progress flow to ask for a
        # human, a callback, or to cancel outright - checked before trying
        # to interpret the reply as whatever slot we were waiting for.
        if intent_guess in INTERRUPT_INTENTS:
            return intent_guess, _start_intent(ctx, intent_guess, text)

        handler = workflows.STEP_HANDLERS.get(session.workflow_step)
        if handler is not None:
            return session.intent or UNKNOWN, handler(ctx, text)
        logger.warning(
            "[conversation] session %s has unknown workflow_step %r",
            session.id,
            session.workflow_step,
        )

    return intent_guess, _start_intent(ctx, intent_guess, text)


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
    """GENERAL_QUERY / UNKNOWN - re-prompt once, then hand off rather than
    looping forever (Part 19's "repeated failed intent detection")."""
    count = int(ctx.slots.get("unresolved_count", 0)) + 1
    if count >= _MAX_UNRESOLVED_TURNS:
        return StepResult(
            response_text="I'll get a member of staff to help with that.",
            workflow_step=None,
            needs_human=True,
            handoff_reason="Repeated failed intent detection.",
        )
    return StepResult(
        response_text=(
            "Sorry, I didn't quite follow that. I can help you book, check, "
            "cancel or reschedule an appointment, or connect you with the team - "
            "what would you like to do?"
        ),
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
