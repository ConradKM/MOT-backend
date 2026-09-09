"""Read-side queries for the garage-facing Communications UI.

Separate from service.py, which only ever *writes* CommunicationLog rows
(sends, inbound logging, status updates) - this module only reads,
aggregates and paginates what's already there. Every function takes a
``garage`` and filters by its id; none of them ever return another tenant's
rows.

Public phone identifiers returned/accepted here are always plain E.164 (no
"whatsapp:" prefix) - that's an internal storage detail of
``CommunicationLog.from_address``/``to_address`` (and of the Twilio API
itself) that stops at this module. Callers work with real phone numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)
from app.models.customer import Customer

from .config import garage_communications_enabled, is_twilio_configured
from .voice_calling import browser_calling_configured

# The garage's own definition of "missed" - a Twilio CallStatus for an
# inbound call that never connected. Not exhaustive of every Twilio value,
# just the ones that mean "nobody answered this".
MISSED_CALL_STATUSES = ("no-answer", "busy", "failed", "canceled")

# The external_provider on a conversation-engine transcript-turn row - a
# message *inside* a call, not a call. Every physical call has exactly one
# call-level VOICE row (from app/communications/service.py, provider
# "twilio"); the ConversationRelay turns for it carry this provider instead
# and must never be counted or listed as calls of their own.
ENGINE_PROVIDER = "comaz_conversation_engine"

# A VOICE row that represents a physical call rather than one of its
# transcript turns.
_CALL_LEVEL_ROW = CommunicationLog.external_provider != ENGINE_PROVIDER

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _clamp_limit(limit: int | None) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _whatsapp_address(phone_e164: str) -> str:
    return f"whatsapp:{phone_e164}"


def _strip_whatsapp_prefix(address: str | None) -> str | None:
    return address.removeprefix("whatsapp:") if address else address


def capabilities_for(garage) -> dict:
    """What this garage can actually do right now - the frontend drives its
    empty/disabled states from this rather than guessing client-side."""
    settings = garage.communication_settings
    return {
        "communications_enabled": is_twilio_configured() and garage_communications_enabled(garage),
        "voice_number_configured": bool(settings and settings.voice_phone_number),
        "whatsapp_configured": bool(
            settings and (settings.whatsapp_sender or settings.messaging_service_sid)
        ),
        # Staff can place a browser (Voice SDK) call: the deployment has an
        # API key + TwiML App (voice_calling.browser_calling_configured), and
        # this business has an outbound number to use as caller ID.
        "outbound_calling_supported": (
            browser_calling_configured() and bool(settings and settings.voice_phone_number)
        ),
    }


def overview_summary(garage) -> dict:
    now = datetime.now(UTC)
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)

    base = CommunicationLog.query.filter_by(garage_id=garage.id)

    calls_today = base.filter(
        CommunicationLog.channel == CHANNEL_VOICE,
        _CALL_LEVEL_ROW,
        CommunicationLog.created_at >= today_start,
    ).count()

    missed_calls_today = base.filter(
        CommunicationLog.channel == CHANNEL_VOICE,
        _CALL_LEVEL_ROW,
        CommunicationLog.direction == DIRECTION_INBOUND,
        CommunicationLog.status.in_(MISSED_CALL_STATUSES),
        CommunicationLog.created_at >= today_start,
    ).count()

    whatsapp_unread = base.filter(
        CommunicationLog.channel == CHANNEL_WHATSAPP,
        CommunicationLog.direction == DIRECTION_INBOUND,
        CommunicationLog.read_at.is_(None),
    ).count()

    outgoing_contacts_today = (
        db.session.query(func.count(func.distinct(CommunicationLog.to_address)))
        .filter(
            CommunicationLog.garage_id == garage.id,
            CommunicationLog.direction == DIRECTION_OUTBOUND,
            CommunicationLog.created_at >= today_start,
            CommunicationLog.to_address.isnot(None),
        )
        .scalar()
        or 0
    )

    # One entry per voice call (its call-level row), not per transcript turn;
    # WhatsApp stays message-oriented.
    recent = (
        base.filter(
            or_(
                CommunicationLog.channel == CHANNEL_WHATSAPP,
                and_(CommunicationLog.channel == CHANNEL_VOICE, _CALL_LEVEL_ROW),
            )
        )
        .order_by(CommunicationLog.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "calls_today": calls_today,
        "missed_calls_today": missed_calls_today,
        "whatsapp_unread": whatsapp_unread,
        "outgoing_contacts_today": outgoing_contacts_today,
        "recent": recent,
        "capabilities": capabilities_for(garage),
    }


def unread_whatsapp_count(garage) -> int:
    """Cheap on its own (no recent-list/summary work) - what the nav badge
    polls, separately from the full overview page."""
    count: int = CommunicationLog.query.filter_by(
        garage_id=garage.id,
        channel=CHANNEL_WHATSAPP,
        direction=DIRECTION_INBOUND,
        read_at=None,
    ).count()
    return count


def list_calls(
    garage,
    *,
    direction: str | None = None,
    missed_only: bool = False,
    search: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[CommunicationLog], int]:
    query = CommunicationLog.query.filter_by(garage_id=garage.id, channel=CHANNEL_VOICE).filter(
        _CALL_LEVEL_ROW
    )

    if search:
        query = query.outerjoin(Customer, CommunicationLog.customer_id == Customer.id)
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                Customer.first_name.ilike(pattern),
                Customer.last_name.ilike(pattern),
                CommunicationLog.from_address.ilike(pattern),
                CommunicationLog.to_address.ilike(pattern),
            )
        )

    if direction:
        query = query.filter(CommunicationLog.direction == direction)

    if missed_only:
        query = query.filter(
            CommunicationLog.direction == DIRECTION_INBOUND,
            CommunicationLog.status.in_(MISSED_CALL_STATUSES),
        )

    total = query.count()
    items = (
        query.order_by(CommunicationLog.created_at.desc())
        .limit(_clamp_limit(limit))
        .offset(max(0, offset))
        .all()
    )
    return items, total


def get_call_detail(garage, call_id) -> CommunicationLog | None:
    result: CommunicationLog | None = (
        CommunicationLog.query.filter_by(garage_id=garage.id, channel=CHANNEL_VOICE, id=call_id)
        .filter(_CALL_LEVEL_ROW)
        .first()
    )
    return result


def get_call_transcript(garage, call: CommunicationLog) -> list[CommunicationLog]:
    """The conversation-engine transcript turns that belong to ``call``,
    oldest first. Grouped by the call's Twilio CallSid *within this garage*
    only - a CallSid is unique per Twilio account, but the query is tenant
    scoped regardless so grouping can never cross businesses.

    ``external_id LIKE '<sid>:%'`` is a fallback for turn rows written before
    the ``call_sid`` column existed (only the inbound side carried the SID,
    embedded in external_id)."""
    call_sid = call.call_sid or call.external_id
    if not call_sid:
        return []
    rows: list[CommunicationLog] = (
        CommunicationLog.query.filter_by(garage_id=garage.id, channel=CHANNEL_VOICE)
        .filter(
            CommunicationLog.external_provider == ENGINE_PROVIDER,
            or_(
                CommunicationLog.call_sid == call_sid,
                CommunicationLog.external_id.like(f"{call_sid}:%"),
            ),
        )
        .order_by(CommunicationLog.created_at.asc())
        .all()
    )
    return rows


def list_conversations(
    garage, *, search: str | None = None, limit: int | None = None, offset: int = 0
) -> tuple[list[dict], int]:
    """Every WhatsApp thread for this garage, most recently active first.

    Grouped in Python from CommunicationLog rows rather than a dedicated
    Conversation table or a windowed SQL query - the simplest correct thing
    at the scale one garage's WhatsApp history actually reaches (the
    (garage_id, created_at) index still keeps the underlying scan cheap). If
    that ever stops being true, this is the one function that would need a
    real window-function query or a materialised conversation table -
    nothing else in the codebase would need to change.
    """
    rows = (
        CommunicationLog.query.filter_by(garage_id=garage.id, channel=CHANNEL_WHATSAPP)
        .order_by(CommunicationLog.created_at.desc())
        .all()
    )

    conversations: dict[str, dict] = {}
    for row in rows:
        counterpart = row.from_address if row.direction == DIRECTION_INBOUND else row.to_address
        if not counterpart:
            continue

        convo = conversations.get(counterpart)
        if convo is None:
            convo = {
                "phone": _strip_whatsapp_prefix(counterpart),
                "customer": row.customer,
                "last_message": row,
                "unread_count": 0,
            }
            conversations[counterpart] = convo
        elif convo["customer"] is None and row.customer is not None:
            convo["customer"] = row.customer

        if row.direction == DIRECTION_INBOUND and row.read_at is None:
            convo["unread_count"] += 1

    result = list(conversations.values())

    if search:
        pattern = search.strip().lower()

        def _matches(convo: dict) -> bool:
            customer = convo["customer"]
            name = f"{customer.first_name} {customer.last_name}".lower() if customer else ""
            return pattern in name or pattern in (convo["phone"] or "").lower()

        result = [c for c in result if _matches(c)]

    total = len(result)
    start = max(0, offset)
    end = start + _clamp_limit(limit)
    return result[start:end], total


def get_conversation_messages(
    garage, phone_e164: str, *, limit: int | None = None
) -> list[CommunicationLog]:
    """Chronological (oldest first) - the most recent ``limit`` messages,
    reversed back into reading order."""
    address = _whatsapp_address(phone_e164)
    rows = (
        CommunicationLog.query.filter_by(garage_id=garage.id, channel=CHANNEL_WHATSAPP)
        .filter(
            or_(CommunicationLog.from_address == address, CommunicationLog.to_address == address)
        )
        .order_by(CommunicationLog.created_at.desc())
        .limit(_clamp_limit(limit))
        .all()
    )
    return list(reversed(rows))


def mark_conversation_read(garage, phone_e164: str) -> int:
    """Marks every unread inbound message in this thread read. Returns how
    many rows changed (0 is a normal, valid outcome - already read, or an
    outbound-only/nonexistent thread)."""
    address = _whatsapp_address(phone_e164)
    updated: int = CommunicationLog.query.filter(
        CommunicationLog.garage_id == garage.id,
        CommunicationLog.channel == CHANNEL_WHATSAPP,
        CommunicationLog.direction == DIRECTION_INBOUND,
        CommunicationLog.from_address == address,
        CommunicationLog.read_at.is_(None),
    ).update({"read_at": datetime.now(UTC)}, synchronize_session=False)
    db.session.commit()
    return updated


def list_customer_communications(
    garage, customer_id, *, limit: int | None = None
) -> list[CommunicationLog]:
    """A customer's calls + WhatsApp messages, most recent first - matched by
    the FK set at log time (see service.py), not by re-matching phone
    strings, so this can never accidentally include another customer's rows.
    """
    rows: list[CommunicationLog] = (
        CommunicationLog.query.filter_by(garage_id=garage.id, customer_id=customer_id)
        .filter(CommunicationLog.channel.in_((CHANNEL_VOICE, CHANNEL_WHATSAPP)))
        .order_by(CommunicationLog.created_at.desc())
        .limit(_clamp_limit(limit))
        .all()
    )
    return rows
