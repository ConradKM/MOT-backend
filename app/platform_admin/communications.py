"""What Platform Admin *shows* about a business's communications setup.

Read-only and derived. Every state transition belongs to
``app/communications/provisioning/service.py``; this module turns the two
rows that service writes (``GarageCommunicationSettings`` and
``GarageCommunicationsOnboarding``) into the two views the console renders:

* :func:`communications_overview` - one row per business for the Operations >
  Communications Setup tab, built in a fixed number of queries rather than one
  per business, the same discipline ``app/platform_admin/onboarding.py``
  already applies to the tenant list.
* :func:`communications_detail` - one business's full Voice and WhatsApp
  workflow, including its recent provider failures.

**Recent failures are read from ``communication_logs``**, the table the whole
platform already writes every send and every inbound webhook to. There is no
second failure store: Operations > Failures and this page are two questions
asked of one set of rows, which is what makes the click-through from a failure
to a business's setup land on the same truth.

Available actions are computed here too, from state. A button that cannot
work is not rendered - "Register sender" before Meta has returned a WABA is
not a disabled control, it is an action that does not exist yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.communications.provisioning import states
from app.communications.provisioning.errors import explain
from app.communications.provisioning.subaccounts import has_credential
from app.communications.provisioning.voice import webhook_urls as voice_webhook_urls
from app.communications.provisioning.whatsapp import (
    describe_existing_registration,
    embedded_signup_prerequisites,
)
from app.communications.provisioning.whatsapp import webhook_urls as whatsapp_webhook_urls
from app.communications.secrets import secrets_configured
from app.conversation.automation import is_conversation_automation_enabled
from app.extensions import db
from app.models.communications.comms_onboarding import GarageCommunicationsOnboarding
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage

#: The same free-text provider statuses Operations counts as failures, so one
#: definition of "failed" serves both pages.
from app.platform_admin.stats import FAILED_STATUSES

CHANNELS = (CHANNEL_VOICE, CHANNEL_WHATSAPP)

# --------------------------------------------------------------------------
# Action keys - the contextual buttons the console may render
# --------------------------------------------------------------------------

ACTION_CREATE_SUBACCOUNT = "create_subaccount"
ACTION_ATTACH_SUBACCOUNT = "attach_subaccount"
ACTION_BUY_VOICE_NUMBER = "buy_voice_number"
ACTION_CONFIGURE_VOICE = "configure_voice"
ACTION_SET_VOICE_ROUTING = "set_voice_routing"
ACTION_TEST_VOICE = "test_voice"
ACTION_MARK_VOICE_ONLINE = "mark_voice_online"
ACTION_CONNECT_WHATSAPP = "connect_whatsapp"
ACTION_MARK_MIGRATION_COMPLETE = "mark_migration_complete"
ACTION_CONTINUE_META_SETUP = "continue_meta_setup"
ACTION_REGISTER_SENDER = "register_sender"
ACTION_SUBMIT_OTP = "submit_otp"
ACTION_CHECK_STATUS = "check_status"
ACTION_RECONFIGURE_WHATSAPP = "reconfigure_whatsapp_webhooks"
ACTION_RUN_TEST_MESSAGE = "run_test_message"
ACTION_RETRY_SETUP = "retry_setup"
ACTION_ENABLE_AUTOMATION = "enable_automation"
ACTION_DISABLE_AUTOMATION = "disable_automation"
ACTION_DISABLE_COMMUNICATIONS = "disable_communications"
ACTION_ENABLE_COMMUNICATIONS = "enable_communications"

#: Wording lives with the key so the console never invents its own label for
#: an action, and a renamed button changes in one place.
ACTION_LABELS: dict[str, str] = {
    ACTION_CREATE_SUBACCOUNT: "Create Twilio subaccount",
    ACTION_ATTACH_SUBACCOUNT: "Attach existing subaccount",
    ACTION_BUY_VOICE_NUMBER: "Buy voice number",
    ACTION_CONFIGURE_VOICE: "Configure voice",
    ACTION_SET_VOICE_ROUTING: "Set escalation and fallback",
    ACTION_TEST_VOICE: "Test voice",
    ACTION_MARK_VOICE_ONLINE: "Mark voice online",
    ACTION_CONNECT_WHATSAPP: "Connect WhatsApp",
    ACTION_MARK_MIGRATION_COMPLETE: "Mark migration complete",
    ACTION_CONTINUE_META_SETUP: "Continue Meta setup",
    ACTION_REGISTER_SENDER: "Register sender",
    ACTION_SUBMIT_OTP: "Enter verification code",
    ACTION_CHECK_STATUS: "Check status",
    ACTION_RECONFIGURE_WHATSAPP: "Reconfigure WhatsApp webhooks",
    ACTION_RUN_TEST_MESSAGE: "Run test message",
    ACTION_RETRY_SETUP: "Retry setup",
    ACTION_ENABLE_AUTOMATION: "Enable automation",
    ACTION_DISABLE_AUTOMATION: "Disable automation",
    ACTION_DISABLE_COMMUNICATIONS: "Disable communications",
    ACTION_ENABLE_COMMUNICATIONS: "Enable communications",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _action(key: str) -> dict:
    return {"key": key, "label": ACTION_LABELS[key]}


# --------------------------------------------------------------------------
# Per-channel views
# --------------------------------------------------------------------------


def _voice_view(
    garage: Garage,
    settings: GarageCommunicationSettings | None,
    row: GarageCommunicationsOnboarding | None,
) -> dict:
    status = row.voice_status if row else states.VOICE_NOT_STARTED
    meaning = states.voice_meaning(status)
    capabilities = (row.voice_number_capabilities if row else None) or ""

    return {
        "status": status,
        "status_label": meaning.label,
        "display_status": meaning.display,
        "display_label": states.DISPLAY_LABELS[meaning.display],
        "blocker": meaning.blocker,
        "next_admin_action": meaning.admin_action,
        "next_customer_action": meaning.customer_action,
        "phone_number": settings.voice_phone_number if settings else None,
        "number_sid": (settings.voice_number_sid if settings else None)
        or (row.voice_number_sid if row else None),
        "capabilities": [part for part in capabilities.split(",") if part],
        "webhooks_configured": bool(row.voice_webhooks_configured) if row else False,
        "webhooks_configured_at": row.voice_webhooks_configured_at if row else None,
        "escalation_number": settings.voice_escalation_number if settings else None,
        "fallback_number": settings.voice_fallback_number if settings else None,
        "last_test_call_sid": row.voice_last_test_call_sid if row else None,
        "last_test_status": row.voice_last_test_status if row else None,
        "last_test_at": row.voice_last_test_at if row else None,
        "online_at": row.voice_online_at if row else None,
        "last_error": _last_error(
            row.voice_last_error_code if row else None,
            row.voice_last_error_message if row else None,
            CHANNEL_VOICE,
        ),
    }


def _whatsapp_view(
    garage: Garage,
    settings: GarageCommunicationSettings | None,
    row: GarageCommunicationsOnboarding | None,
) -> dict:
    status = row.whatsapp_status if row else states.WA_NOT_STARTED
    meaning = states.whatsapp_meaning(status)

    return {
        "status": status,
        "status_label": meaning.label,
        "display_status": meaning.display,
        "display_label": states.DISPLAY_LABELS[meaning.display],
        "blocker": meaning.blocker,
        "next_admin_action": meaning.admin_action,
        "next_customer_action": meaning.customer_action,
        "phone_number": row.whatsapp_number if row else None,
        "sender_address": settings.whatsapp_sender if settings else None,
        "number_already_in_use": bool(row.whatsapp_number_in_use) if row else False,
        "waba_id": row.waba_id if row else None,
        "meta_business_id": row.meta_business_id if row else None,
        "meta_phone_number_id": row.meta_phone_number_id if row else None,
        "meta_signup_started_at": row.meta_signup_started_at if row else None,
        "meta_signup_completed_at": row.meta_signup_completed_at if row else None,
        "sender_sid": row.whatsapp_sender_sid if row else None,
        "sender_status": row.whatsapp_sender_status if row else None,
        "display_name": row.whatsapp_display_name if row else None,
        "offline_reason": row.whatsapp_offline_reason if row else None,
        "last_status_check_at": row.whatsapp_last_status_check_at if row else None,
        "last_test_at": row.whatsapp_last_test_at if row else None,
        "last_test_status": row.whatsapp_last_test_status if row else None,
        "online_at": row.whatsapp_online_at if row else None,
        "last_error": _last_error(
            row.whatsapp_last_error_code if row else None,
            row.whatsapp_last_error_message if row else None,
            CHANNEL_WHATSAPP,
        ),
        # Only populated while the business is actually blocked on it, so the
        # console never shows migration instructions to someone who doesn't
        # need them.
        "existing_registration": (
            describe_existing_registration(row.whatsapp_number or "")
            if row and row.whatsapp_status == states.WA_EXISTING_MIGRATION_REQUIRED
            else None
        ),
    }


def _last_error(code: str | None, message: str | None, channel: str) -> dict | None:
    """The last provider failure for a channel, explained but never rewritten -
    ``explain`` keeps the original code and message alongside its guidance."""
    if not code and not message:
        return None
    return explain(code, message, channel=channel)


# --------------------------------------------------------------------------
# Available actions
# --------------------------------------------------------------------------


def _voice_actions(
    settings: GarageCommunicationSettings | None, row: GarageCommunicationsOnboarding | None
) -> list[dict]:
    status = row.voice_status if row else states.VOICE_NOT_STARTED
    has_subaccount = bool(settings and settings.twilio_subaccount_sid)
    has_number = bool(settings and settings.voice_phone_number)

    actions: list[str] = []
    if not has_subaccount:
        actions += [ACTION_CREATE_SUBACCOUNT, ACTION_ATTACH_SUBACCOUNT]
    elif not has_number:
        actions.append(ACTION_BUY_VOICE_NUMBER)
    else:
        actions.append(ACTION_CONFIGURE_VOICE)
        actions.append(ACTION_SET_VOICE_ROUTING)
        actions.append(ACTION_TEST_VOICE)
        if status != states.VOICE_ONLINE and row and row.voice_webhooks_configured:
            actions.append(ACTION_MARK_VOICE_ONLINE)

    if status in (states.VOICE_FAILED, states.VOICE_ACTION_REQUIRED):
        actions.append(ACTION_RETRY_SETUP)

    return [_action(key) for key in _dedupe(actions)]


def _whatsapp_actions(
    settings: GarageCommunicationSettings | None, row: GarageCommunicationsOnboarding | None
) -> list[dict]:
    status = row.whatsapp_status if row else states.WA_NOT_STARTED
    has_subaccount = bool(settings and settings.twilio_subaccount_sid)

    actions: list[str] = []
    if status in (states.WA_NOT_STARTED, states.WA_DISABLED):
        actions.append(ACTION_CONNECT_WHATSAPP)
    elif status == states.WA_EXISTING_MIGRATION_REQUIRED:
        actions += [ACTION_MARK_MIGRATION_COMPLETE, ACTION_CONNECT_WHATSAPP]
    elif status in (
        states.WA_NUMBER_ENTERED,
        states.WA_META_SIGNUP_REQUIRED,
        states.WA_META_SIGNUP_IN_PROGRESS,
    ):
        actions += [ACTION_CONTINUE_META_SETUP, ACTION_CONNECT_WHATSAPP]
    elif status in (states.WA_META_SIGNUP_COMPLETED, states.WA_WABA_RECEIVED):
        actions.append(ACTION_REGISTER_SENDER if has_subaccount else ACTION_CREATE_SUBACCOUNT)
    elif status == states.WA_TWILIO_SUBACCOUNT_READY:
        actions.append(ACTION_REGISTER_SENDER)
    elif status == states.WA_OTP_REQUIRED:
        actions += [ACTION_SUBMIT_OTP, ACTION_CHECK_STATUS]
    elif status in (
        states.WA_SENDER_REGISTRATION_PENDING,
        states.WA_SENDER_REGISTERING,
        states.WA_TESTING,
    ):
        actions.append(ACTION_CHECK_STATUS)
    elif status == states.WA_ONLINE:
        actions += [ACTION_RUN_TEST_MESSAGE, ACTION_CHECK_STATUS, ACTION_RECONFIGURE_WHATSAPP]
    elif status in (states.WA_ACTION_REQUIRED, states.WA_FAILED):
        actions += [ACTION_CHECK_STATUS, ACTION_RETRY_SETUP]

    return [_action(key) for key in _dedupe(actions)]


def _business_actions(garage: Garage, settings: GarageCommunicationSettings | None) -> list[dict]:
    enabled = bool(settings and settings.communications_enabled)
    automation_on = is_conversation_automation_enabled(garage)
    keys = [
        ACTION_DISABLE_COMMUNICATIONS if enabled else ACTION_ENABLE_COMMUNICATIONS,
        ACTION_DISABLE_AUTOMATION if automation_on else ACTION_ENABLE_AUTOMATION,
    ]
    return [_action(key) for key in keys]


def _dedupe(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# --------------------------------------------------------------------------
# Recent provider failures, from the existing communication log
# --------------------------------------------------------------------------


def recent_channel_errors(garage: Garage, *, days: int = 30, limit: int = 20) -> list[dict]:
    """This business's recent failed Voice/WhatsApp communications, explained.

    Reuses ``communication_logs``: the rows Operations > Failures already
    counts. Clicking a failure there and landing here shows the *same* rows,
    because there is only one place failures are recorded.
    """
    since = _utcnow() - timedelta(days=days)
    rows = (
        db.session.execute(
            select(CommunicationLog)
            .where(
                CommunicationLog.garage_id == garage.id,
                CommunicationLog.channel.in_(CHANNELS),
                CommunicationLog.created_at >= since,
                or_(
                    CommunicationLog.status.in_(FAILED_STATUSES),
                    CommunicationLog.error_code.isnot(None),
                ),
            )
            .order_by(CommunicationLog.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
        .scalars()
        .all()
    )

    return [
        {
            "id": log.id,
            "channel": log.channel,
            "direction": log.direction,
            "status": log.status,
            "to_address": log.to_address,
            "created_at": log.created_at,
            "trigger_event": log.trigger_event,
            **explain(log.error_code, log.error_message, channel=log.channel),
        }
        for log in rows
    ]


def _failure_counts(garage_ids: list[uuid.UUID], *, days: int) -> dict[uuid.UUID, int]:
    """Failed Voice/WhatsApp counts per business, in one grouped query."""
    if not garage_ids:
        return {}
    since = _utcnow() - timedelta(days=days)
    rows = db.session.execute(
        select(CommunicationLog.garage_id, func.count())
        .where(
            CommunicationLog.garage_id.in_(garage_ids),
            CommunicationLog.channel.in_(CHANNELS),
            CommunicationLog.created_at >= since,
            CommunicationLog.status.in_(FAILED_STATUSES),
        )
        .group_by(CommunicationLog.garage_id)
    ).all()
    return {row[0]: row[1] for row in rows}


# --------------------------------------------------------------------------
# The two views
# --------------------------------------------------------------------------


def _overall_status(
    voice: dict, whatsapp: dict, settings: GarageCommunicationSettings | None
) -> str:
    """One status for the business, from its two channels and the master switch.

    ``states.overall_display`` deliberately lets a live channel outrank a
    disabled one, so a half-disabled business is described by whatever is
    still running. The master switch is the exception: with
    ``communications_enabled`` off, *nothing* sends, so a channel still
    sitting at "setup required" would invite an operator to keep provisioning
    a business that is switched off. When the switch is off and a channel was
    deliberately turned off with it, the honest single word is Disabled.

    A business that has simply never been set up is untouched by this: both
    its channels are NOT_STARTED rather than DISABLED, so it still reports
    "Setup required" as it should.
    """
    overall = states.overall_display(voice["display_status"], whatsapp["display_status"])
    switched_off = settings is not None and not settings.communications_enabled
    deliberately_disabled = states.DISPLAY_DISABLED in (
        voice["display_status"],
        whatsapp["display_status"],
    )
    if switched_off and deliberately_disabled:
        return states.DISPLAY_DISABLED
    return overall


def _lead_channel(voice: dict, whatsapp: dict, overall: str) -> dict:
    """The channel a one-line summary should speak for.

    Whichever channel the business's overall status came from - so the blocker
    and next actions on that row describe the thing that is actually holding
    the business up, rather than an average of two channels. When both match
    (both online, both stuck the same way) voice wins arbitrarily; they are
    saying the same thing.
    """
    for channel in (voice, whatsapp):
        if channel["display_status"] == overall:
            return channel
    # Only reachable when the master switch forced DISABLED - in which case at
    # least one channel is disabled and has no blocker to report anyway.
    return voice


def _summary_row(
    garage: Garage,
    settings: GarageCommunicationSettings | None,
    row: GarageCommunicationsOnboarding | None,
    *,
    automation_enabled: bool,
    failures: int,
) -> dict:
    voice = _voice_view(garage, settings, row)
    wa = _whatsapp_view(garage, settings, row)
    overall = _overall_status(voice, wa, settings)

    lead = _lead_channel(voice, wa, overall)

    return {
        "garage_id": garage.id,
        "garage_name": garage.name,
        "garage_slug": garage.slug,
        "communications_enabled": bool(settings and settings.communications_enabled),
        "automation_enabled": automation_enabled,
        "twilio_subaccount_sid": settings.twilio_subaccount_sid if settings else None,
        "subaccount_state": _subaccount_state(settings),
        "voice_phone_number": voice["phone_number"],
        "voice_status": voice["status"],
        "voice_display_status": voice["display_status"],
        "whatsapp_number": wa["phone_number"],
        "whatsapp_status": wa["status"],
        "whatsapp_display_status": wa["display_status"],
        "waba_id": wa["waba_id"],
        "sender_status": wa["sender_status"],
        "webhooks_configured": voice["webhooks_configured"],
        "display_status": overall,
        "display_label": states.DISPLAY_LABELS[overall],
        "setup_stage": lead["status_label"],
        "blocker": lead["blocker"],
        "next_admin_action": lead["next_admin_action"],
        "next_customer_action": lead["next_customer_action"],
        "last_error": voice["last_error"] or wa["last_error"],
        "recent_failures": failures,
        "updated_at": row.updated_at if row else None,
    }


def _subaccount_state(settings: GarageCommunicationSettings | None) -> str:
    """Three-valued, because "we have a SID" and "we can act as it" are
    different facts and only the second lets us register a sender."""
    if settings is None or not settings.twilio_subaccount_sid:
        return "NONE"
    return "READY" if subaccount_credential_present_for(settings) else "CREDENTIAL_MISSING"


def subaccount_credential_present_for(settings: GarageCommunicationSettings) -> bool:
    return has_credential(settings.twilio_subaccount_sid)


def communications_overview(*, search: str | None = None, status: str | None = None) -> dict:
    """Every business and its communications onboarding state.

    Not paginated: this is an operator's worklist, and the useful reading is
    "which of my businesses are stuck", which a page break would hide. It is
    built from four queries regardless of how many businesses there are.
    """
    query = select(Garage).order_by(Garage.name)
    if search:
        query = query.where(Garage.name.ilike(f"%{search.strip()}%"))
    garages = list(db.session.execute(query).scalars().all())
    garage_ids = [g.id for g in garages]

    settings_by_garage = {
        row.garage_id: row
        for row in db.session.execute(
            select(GarageCommunicationSettings).where(
                GarageCommunicationSettings.garage_id.in_(garage_ids)
            )
        )
        .scalars()
        .all()
    }
    onboarding_by_garage = {
        row.garage_id: row
        for row in db.session.execute(
            select(GarageCommunicationsOnboarding).where(
                GarageCommunicationsOnboarding.garage_id.in_(garage_ids)
            )
        )
        .scalars()
        .all()
    }
    failures = _failure_counts(garage_ids, days=7)
    automation = _automation_by_garage(garage_ids)

    items = [
        _summary_row(
            garage,
            settings_by_garage.get(garage.id),
            onboarding_by_garage.get(garage.id),
            automation_enabled=automation.get(garage.id, False),
            failures=failures.get(garage.id, 0),
        )
        for garage in garages
    ]
    if status:
        items = [item for item in items if item["display_status"] == status]

    counts: dict[str, int] = {key: 0 for key in states.DISPLAY_STATUSES}
    for item in items:
        counts[item["display_status"]] = counts.get(item["display_status"], 0) + 1

    return {
        "items": items,
        "total": len(items),
        "counts_by_status": counts,
        "platform": platform_readiness(),
    }


def _automation_by_garage(garage_ids: list[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """The conversation-automation flag for many businesses in one query -
    ``is_conversation_automation_enabled`` is per-garage by design and would
    be N queries here."""
    if not garage_ids:
        return {}
    from app.models.communications.automation_settings import (
        GarageCommunicationAutomationSettings,
    )

    rows = db.session.execute(
        select(
            GarageCommunicationAutomationSettings.garage_id,
            GarageCommunicationAutomationSettings.conversation_automation_enabled,
        ).where(GarageCommunicationAutomationSettings.garage_id.in_(garage_ids))
    ).all()
    return {row[0]: bool(row[1]) for row in rows}


def platform_readiness() -> dict:
    """Deployment-level prerequisites, reported once rather than per business.

    Every one of these blocks *all* businesses, so showing it on each row
    would be noise - but not showing it at all would leave an operator
    clicking a button that cannot work.
    """
    from app.communications.config import is_twilio_configured
    from app.communications.provisioning.voice import webhooks_reachable

    return {
        "twilio_configured": is_twilio_configured(),
        "secrets_configured": secrets_configured(),
        "webhooks_reachable": webhooks_reachable(),
        "embedded_signup": embedded_signup_prerequisites(),
        "voice_webhooks": voice_webhook_urls(),
        "whatsapp_webhooks": whatsapp_webhook_urls(),
    }


def communications_detail(garage: Garage) -> dict:
    """One business's full communications setup - the detail page's read."""
    settings = garage.communication_settings
    row = garage.communications_onboarding
    voice = _voice_view(garage, settings, row)
    wa = _whatsapp_view(garage, settings, row)
    overall = _overall_status(voice, wa, settings)

    return {
        "garage_id": garage.id,
        "garage_name": garage.name,
        "garage_slug": garage.slug,
        "communications_enabled": bool(settings and settings.communications_enabled),
        "automation_enabled": is_conversation_automation_enabled(garage),
        "twilio_subaccount_sid": settings.twilio_subaccount_sid if settings else None,
        "subaccount_state": _subaccount_state(settings),
        "messaging_service_sid": settings.messaging_service_sid if settings else None,
        "display_status": overall,
        "display_label": states.DISPLAY_LABELS[overall],
        "voice": voice,
        "whatsapp": wa,
        "voice_actions": _voice_actions(settings, row),
        "whatsapp_actions": _whatsapp_actions(settings, row),
        "business_actions": _business_actions(garage, settings),
        "recent_errors": recent_channel_errors(garage),
        "notes": row.notes if row else None,
        "platform": platform_readiness(),
        "updated_at": row.updated_at if row else None,
    }
