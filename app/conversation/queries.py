"""Staff/owner-facing reads (and simple staff-triggered status changes) over
the conversation engine's own data - callback requests and per-conversation
session state.

Deliberately separate from actions.py, which is the safe domain "tool" layer
the conversation engine (and any future AI intent resolver) is allowed to
call: nothing here is reachable from a customer's message. This module only
ever serves the authenticated Communications API
(app/communications/routes.py), the same way app/communications/queries.py
serves the rest of that API.
"""

from __future__ import annotations

from app.extensions import db
from app.models.communications.communication_log import CHANNEL_WHATSAPP
from app.models.conversation.callback_request import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    CallbackRequest,
)
from app.models.conversation.conversation_session import ConversationSession


def list_callback_requests(
    garage, *, status: str | None = None, limit: int | None = None, offset: int = 0
) -> tuple[list[CallbackRequest], int]:
    query = CallbackRequest.query.filter_by(garage_id=garage.id)
    if status:
        query = query.filter(CallbackRequest.status == status)
    total = query.count()
    query = query.order_by(CallbackRequest.created_at.desc())
    if limit is not None:
        query = query.offset(offset).limit(limit)
    return query.all(), total


def get_callback_request(garage, callback_id) -> CallbackRequest | None:
    return CallbackRequest.query.filter_by(garage_id=garage.id, id=callback_id).first()


def complete_callback_request(callback: CallbackRequest) -> None:
    callback.status = STATUS_COMPLETED
    db.session.commit()


def cancel_callback_request(callback: CallbackRequest) -> None:
    callback.status = STATUS_CANCELLED
    db.session.commit()


def get_conversation_session(
    garage, phone_e164: str, *, channel: str = CHANNEL_WHATSAPP
) -> ConversationSession | None:
    """The most recent session behind this phone's conversation, if any - so
    the staff "take over" / "resume automation" controls know what state
    they're acting on (and act on the right row, not a stale earlier one)."""
    return (
        ConversationSession.query.filter_by(
            garage_id=garage.id, channel=channel, customer_phone=phone_e164
        )
        .order_by(ConversationSession.created_at.desc())
        .first()
    )
