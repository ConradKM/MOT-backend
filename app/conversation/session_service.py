"""Conversation session lifecycle - loading, updating, expiring, and human
handoff/takeover. Every write here is the explicit, structured state
described in models/conversation/conversation_session.py's docstring; this
module never stores anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models.conversation.conversation_session import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_HUMAN_HANDOFF,
    ConversationSession,
)

# How long a session can sit with no activity before it's considered stale
# (Part 35). Kept short relative to a human support ticket on purpose - a
# booking conversation that goes quiet for half an hour should start fresh
# rather than resume against availability that's no longer current.
SESSION_TIMEOUT_MINUTES = 30


def get_active_session(garage, channel: str, phone_e164: str) -> ConversationSession | None:
    """The session an inbound message should attach to - one still ACTIVE,
    or one already sitting in HUMAN_HANDOFF. A handoff session must keep
    being found on every subsequent message (Part 20) so the bot never
    starts a fresh automated reply alongside - or instead of - the human
    who's now handling it."""
    return (
        ConversationSession.query.filter_by(
            garage_id=garage.id, channel=channel, customer_phone=phone_e164
        )
        .filter(ConversationSession.status.in_([STATUS_ACTIVE, STATUS_HUMAN_HANDOFF]))
        .order_by(ConversationSession.created_at.desc())
        .first()
    )


def is_stale(session: ConversationSession, *, now: datetime | None = None) -> bool:
    """A HUMAN_HANDOFF session never goes stale by timeout - same rule as
    expire_stale_sessions below: only a human resuming automation ends one,
    never a clock. Otherwise the bot could silently start replying again
    while staff still believe they own the conversation."""
    if session.status == STATUS_HUMAN_HANDOFF:
        return False
    now = now or datetime.now(UTC)
    return now - session.last_activity_at > timedelta(minutes=SESSION_TIMEOUT_MINUTES)


def get_or_create_session(
    garage, channel: str, phone_e164: str, *, customer_id=None, now: datetime | None = None
) -> ConversationSession:
    """The session this inbound message belongs to - a fresh one if there's
    no active session, or the existing one has gone stale (Part 35: a
    returning customer after a long gap starts a clean workflow rather than
    resuming a stale booking-in-progress)."""
    now = now or datetime.now(UTC)
    existing = get_active_session(garage, channel, phone_e164)

    if existing is not None and not is_stale(existing, now=now):
        if customer_id is not None and existing.customer_id is None:
            existing.customer_id = customer_id
            db.session.commit()
        return existing

    if existing is not None:
        existing.status = STATUS_EXPIRED
        db.session.commit()

    session = ConversationSession(
        garage_id=garage.id,
        channel=channel,
        customer_phone=phone_e164,
        customer_id=customer_id,
        status=STATUS_ACTIVE,
        context={},
        last_activity_at=now,
    )
    db.session.add(session)
    db.session.commit()
    return session


def touch(session: ConversationSession, *, now: datetime | None = None) -> None:
    session.last_activity_at = now or datetime.now(UTC)
    db.session.commit()


def set_workflow_step(
    session: ConversationSession,
    step: str | None,
    *,
    intent: str | None = None,
    now: datetime | None = None,
    **context_updates,
) -> None:
    """Advance the session's explicit state. ``context_updates`` merges into
    the existing context dict (never replaces it wholesale) so earlier slots
    already filled in this workflow (e.g. the chosen appointment type) stay
    put while later ones (date, then time, then vehicle) are added."""
    session.workflow_step = step
    if intent is not None:
        session.intent = intent
    if context_updates:
        merged = dict(session.context or {})
        merged.update(context_updates)
        session.context = merged
    session.last_activity_at = now or datetime.now(UTC)
    db.session.commit()


def mark_processed(session: ConversationSession, external_message_id: str | None) -> None:
    if external_message_id:
        session.last_external_message_id = external_message_id
        db.session.commit()


def already_processed(session: ConversationSession, external_message_id: str | None) -> bool:
    """True if this exact provider message id already drove this session's
    last step - a duplicate webhook delivery (Part 36) must not repeat
    whatever action it triggered."""
    return bool(external_message_id) and session.last_external_message_id == external_message_id


def complete_session(session: ConversationSession) -> None:
    session.status = STATUS_COMPLETED
    session.workflow_step = None
    db.session.commit()


def handoff_to_human(session: ConversationSession, reason: str, *, now: datetime | None = None) -> None:
    session.status = STATUS_HUMAN_HANDOFF
    session.handoff_reason = reason
    session.last_activity_at = now or datetime.now(UTC)
    db.session.commit()


def resume_automation(session: ConversationSession) -> None:
    """Staff hands a conversation back to automation (Part 20). Clears the
    handoff and starts a clean workflow rather than resuming whatever the
    bot was halfway through before a human took over - the context that led
    to the handoff shouldn't silently keep driving the conversation. Always
    real wall-clock time - unlike the engine's own turn processing, a staff
    click in the UI has no "as of" timestamp to replay."""
    session.status = STATUS_ACTIVE
    session.handoff_reason = None
    session.intent = None
    session.workflow_step = None
    session.context = {}
    session.last_activity_at = datetime.now(UTC)
    db.session.commit()


def expire_stale_sessions(*, now: datetime | None = None) -> int:
    """Sweeps every ACTIVE session past the inactivity timeout to EXPIRED.
    Safe to call often (e.g. from a scheduled task) - a no-op when nothing
    is stale. Human handoff sessions are untouched - a human, not a timer,
    ends those."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    stale_ids = [
        row.id
        for row in ConversationSession.query.filter(
            ConversationSession.status == STATUS_ACTIVE,
            ConversationSession.last_activity_at < cutoff,
        ).all()
    ]
    if not stale_ids:
        return 0
    ConversationSession.query.filter(ConversationSession.id.in_(stale_ids)).update(
        {"status": STATUS_EXPIRED}, synchronize_session=False
    )
    db.session.commit()
    return len(stale_ids)
