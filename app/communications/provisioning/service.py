"""The one place communications onboarding state changes.

Every Platform Admin action lands on a function here. Each one does the same
three things in the same order:

1. call Twilio (or Meta, via the browser) through ``voice.py`` /
   ``whatsapp.py`` / ``subaccounts.py``,
2. move the channel's state and record what the provider said - including,
   on failure, the provider's own error code and message, and
3. where a channel has just gone live, mirror its address into
   :class:`~app.models.communications.garage_communication_settings.GarageCommunicationSettings`,
   because that is the row every inbound webhook resolves a tenant against.

Errors are never swallowed. A provider failure raises
:class:`ProvisioningActionError` **after** the state and the error fields have
been written, so the console shows what happened and the row still says how
the business got there. That is why each action commits its own state before
re-raising.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.conversation import automation
from app.extensions import db
from app.models.communications.comms_onboarding import GarageCommunicationsOnboarding
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage

from . import states, voice, whatsapp
from .subaccounts import (
    SubaccountError,
    adopt_subaccount,
    create_subaccount,
    has_credential,
)
from .voice import VoiceProvisioningError
from .whatsapp import WhatsAppProvisioningError

logger = logging.getLogger(__name__)


class ProvisioningActionError(RuntimeError):
    """An admin action could not be completed. ``code`` is the provider's own
    error code where there was one."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Row access
# --------------------------------------------------------------------------


def get_onboarding(garage: Garage) -> GarageCommunicationsOnboarding | None:
    return garage.communications_onboarding


def ensure_onboarding(garage: Garage) -> GarageCommunicationsOnboarding:
    """This business's onboarding row, created on first use.

    Absent is a real, valid state ("nobody has started"), so the row is only
    materialised when something is actually recorded - the same rule
    ``GarageCommunicationSettings`` already follows.
    """
    row = garage.communications_onboarding
    if row is None:
        row = GarageCommunicationsOnboarding(garage_id=garage.id)
        db.session.add(row)
        db.session.flush()
        garage.communications_onboarding = row
    return row


def ensure_settings(garage: Garage) -> GarageCommunicationSettings:
    settings = garage.communication_settings
    if settings is None:
        settings = GarageCommunicationSettings(garage_id=garage.id)
        db.session.add(settings)
        db.session.flush()
        garage.communication_settings = settings
    return settings


def _clear_voice_error(row: GarageCommunicationsOnboarding) -> None:
    row.voice_last_error_code = None
    row.voice_last_error_message = None


def _clear_whatsapp_error(row: GarageCommunicationsOnboarding) -> None:
    row.whatsapp_last_error_code = None
    row.whatsapp_last_error_message = None


def _fail_voice(row: GarageCommunicationsOnboarding, exc: Exception, *, status: str) -> None:
    row.voice_status = status
    row.voice_last_error_code = getattr(exc, "code", None)
    row.voice_last_error_message = str(exc)
    db.session.commit()


def _fail_whatsapp(row: GarageCommunicationsOnboarding, exc: Exception, *, status: str) -> None:
    row.whatsapp_status = status
    row.whatsapp_last_error_code = getattr(exc, "code", None)
    row.whatsapp_last_error_message = str(exc)
    db.session.commit()


# --------------------------------------------------------------------------
# Subaccount
# --------------------------------------------------------------------------


def action_create_subaccount(garage: Garage) -> GarageCommunicationsOnboarding:
    """Create or adopt this business's dedicated Twilio subaccount.

    Advances *both* channels out of their "no subaccount" state, since they
    share it - a business has one subaccount, not one per channel.
    """
    row = ensure_onboarding(garage)
    ensure_settings(garage)

    if row.voice_status == states.VOICE_NOT_STARTED:
        row.voice_status = states.VOICE_SUBACCOUNT_PENDING

    try:
        create_subaccount(garage)
    except SubaccountError as exc:
        _fail_voice(row, exc, status=states.VOICE_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_voice_error(row)
    if row.voice_status in (states.VOICE_NOT_STARTED, states.VOICE_SUBACCOUNT_PENDING):
        row.voice_status = states.VOICE_SUBACCOUNT_READY
    if row.whatsapp_status == states.WA_WABA_RECEIVED:
        row.whatsapp_status = states.WA_TWILIO_SUBACCOUNT_READY
    db.session.commit()
    return row


def action_attach_subaccount(
    garage: Garage, *, subaccount_sid: str, auth_token: str
) -> GarageCommunicationsOnboarding:
    """Attach a subaccount that already exists in the Twilio console."""
    row = ensure_onboarding(garage)
    ensure_settings(garage)
    try:
        adopt_subaccount(garage, subaccount_sid, auth_token)
    except SubaccountError as exc:
        _fail_voice(row, exc, status=states.VOICE_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc)) from exc

    _clear_voice_error(row)
    if row.voice_status in (states.VOICE_NOT_STARTED, states.VOICE_SUBACCOUNT_PENDING):
        row.voice_status = states.VOICE_SUBACCOUNT_READY
    if row.whatsapp_status == states.WA_WABA_RECEIVED:
        row.whatsapp_status = states.WA_TWILIO_SUBACCOUNT_READY
    db.session.commit()
    return row


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def action_buy_voice_number(
    garage: Garage, *, phone_number: str, already_owned: bool = False
) -> GarageCommunicationsOnboarding:
    """Buy (or adopt) a voice number and assign it to this business.

    The purchase already carries CoMaz's webhook URLs, so a bought number is
    never live-but-unconfigured; the state still passes through
    NUMBER_ASSIGNED → WEBHOOKS_CONFIGURED so the console can show what
    happened rather than jumping two steps silently.
    """
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)

    if not settings.twilio_subaccount_sid:
        raise ProvisioningActionError(
            "Create this business's Twilio subaccount before buying a number."
        )

    previous = row.voice_status
    row.voice_status = states.VOICE_NUMBER_PURCHASING
    db.session.flush()

    try:
        if already_owned:
            result = voice.assign_existing_number(garage, phone_number)
        else:
            result = voice.buy_number(garage, phone_number)
    except (VoiceProvisioningError, SubaccountError) as exc:
        row.voice_status = previous if already_owned else states.VOICE_FAILED
        _fail_voice(row, exc, status=row.voice_status)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_voice_error(row)
    settings.voice_phone_number = result["phone_number"]
    settings.voice_number_sid = result["sid"]
    row.voice_number_sid = result["sid"]
    row.voice_number_capabilities = ",".join(result.get("capabilities") or []) or None
    row.voice_status = states.VOICE_NUMBER_ASSIGNED

    if not already_owned:
        # A freshly bought number left Twilio already pointed at CoMaz.
        row.voice_webhooks_configured = True
        row.voice_webhooks_configured_at = _now()
        row.voice_status = states.VOICE_WEBHOOKS_CONFIGURED

    db.session.commit()
    return row


def action_configure_voice(garage: Garage) -> GarageCommunicationsOnboarding:
    """(Re)point this business's number at CoMaz's voice webhooks."""
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)
    number_sid = settings.voice_number_sid or row.voice_number_sid
    if not number_sid:
        raise ProvisioningActionError("This business has no voice number to configure yet.")

    try:
        result = voice.configure_number(garage, number_sid)
    except (VoiceProvisioningError, SubaccountError) as exc:
        _fail_voice(row, exc, status=states.VOICE_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_voice_error(row)
    settings.voice_phone_number = result["phone_number"]
    row.voice_webhooks_configured = True
    row.voice_webhooks_configured_at = _now()
    # Never demote a live channel: reconfiguring an ONLINE number is a repair,
    # not a restart, and knocking it back to WEBHOOKS_CONFIGURED would make
    # the Communications Setup list lie about a business that is still taking
    # calls.
    if row.voice_status not in (states.VOICE_ONLINE, states.VOICE_DISABLED):
        row.voice_status = states.VOICE_WEBHOOKS_CONFIGURED
    db.session.commit()
    return row


def action_set_voice_routing(
    garage: Garage,
    *,
    escalation_number: str | None = None,
    fallback_number: str | None = None,
) -> GarageCommunicationsOnboarding:
    """Set where a call goes when automation cannot handle it, and where it
    goes if CoMaz itself is unreachable. Both are plain configuration - no
    provider call is involved."""
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)
    settings.voice_escalation_number = escalation_number or None
    settings.voice_fallback_number = fallback_number or None
    db.session.commit()
    return row


def action_test_voice(garage: Garage, *, to_number: str) -> dict:
    """Place a test call and record its outcome against the business."""
    row = ensure_onboarding(garage)
    try:
        result = voice.place_test_call(garage, to_number)
    except (VoiceProvisioningError, SubaccountError) as exc:
        _fail_voice(row, exc, status=states.VOICE_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_voice_error(row)
    row.voice_last_test_call_sid = result["call_sid"]
    row.voice_last_test_status = result["status"]
    row.voice_last_test_at = _now()
    if row.voice_status not in (states.VOICE_ONLINE, states.VOICE_DISABLED):
        row.voice_status = states.VOICE_TESTING
    db.session.commit()
    return result


def action_mark_voice_online(garage: Garage) -> GarageCommunicationsOnboarding:
    """Confirm voice is live for this business.

    Deliberately an explicit operator decision rather than something inferred
    from a test call's status callback: "the number rang" and "this business
    is ready for its customers to call it" are different claims, and only a
    person can make the second one.
    """
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)
    if not settings.voice_phone_number:
        raise ProvisioningActionError("This business has no voice number yet.")
    if not row.voice_webhooks_configured:
        raise ProvisioningActionError(
            "Configure the number's webhooks before marking voice online."
        )
    row.voice_status = states.VOICE_ONLINE
    row.voice_online_at = _now()
    _clear_voice_error(row)
    settings.communications_enabled = True
    db.session.commit()
    return row


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------


def action_set_whatsapp_number(
    garage: Garage, *, number_e164: str, already_on_whatsapp: bool = False
) -> GarageCommunicationsOnboarding:
    """Record the business's own WhatsApp number - always the first step.

    Meta needs the full number known before Embedded Signup starts, and a
    number that is already on consumer WhatsApp routes to the migration state
    instead of to signup, because Meta will refuse the registration until the
    business clears it themselves.
    """
    clash = (
        db.session.query(GarageCommunicationsOnboarding)
        .filter(
            GarageCommunicationsOnboarding.whatsapp_number == number_e164,
            GarageCommunicationsOnboarding.garage_id != garage.id,
        )
        .first()
    )
    if clash is not None:
        # Caught here rather than at the unique index on `whatsapp_sender`,
        # which only bites once the sender goes live - by which point Meta
        # signup has already been done for the wrong business.
        raise ProvisioningActionError(
            f"{number_e164} is already recorded against {clash.garage.name}. "
            "Each business must use its own WhatsApp number."
        )

    row = ensure_onboarding(garage)
    row.whatsapp_number = number_e164
    row.whatsapp_number_in_use = already_on_whatsapp
    _clear_whatsapp_error(row)
    row.whatsapp_status = (
        states.WA_EXISTING_MIGRATION_REQUIRED if already_on_whatsapp else states.WA_NUMBER_ENTERED
    )
    db.session.commit()
    return row


def action_mark_migration_complete(garage: Garage) -> GarageCommunicationsOnboarding:
    """The business has cleared the number's existing WhatsApp registration.

    An operator assertion, not a check: CoMaz has no way to see inside the
    customer's WhatsApp account, and Meta will tell us soon enough if it is
    still occupied (error 63110), at which point the state comes straight back
    here.
    """
    row = ensure_onboarding(garage)
    if row.whatsapp_status != states.WA_EXISTING_MIGRATION_REQUIRED:
        raise ProvisioningActionError(
            "This business is not waiting on an existing WhatsApp registration."
        )
    row.whatsapp_number_in_use = False
    row.whatsapp_status = states.WA_NUMBER_ENTERED
    _clear_whatsapp_error(row)
    db.session.commit()
    return row


def action_start_embedded_signup(garage: Garage) -> dict:
    """Mint the launch configuration for Meta Embedded Signup.

    Returns public identifiers plus a one-time ``state`` nonce that the
    completion call must echo back. Refuses to start when a prerequisite is
    missing, so nobody sends a business into a Meta window that cannot finish.
    """
    row = ensure_onboarding(garage)
    if not row.whatsapp_number:
        raise ProvisioningActionError(
            "Record this business's WhatsApp number before starting Meta signup."
        )
    if row.whatsapp_status == states.WA_EXISTING_MIGRATION_REQUIRED:
        raise ProvisioningActionError(
            "This number is still registered to an existing WhatsApp account. Meta will "
            "refuse signup until the business removes it."
        )
    if not whatsapp.embedded_signup_ready():
        raise ProvisioningActionError(
            "Meta Embedded Signup is not configured for this deployment yet - see the "
            "prerequisites on this page."
        )

    row.meta_signup_state = whatsapp.new_signup_state()
    row.meta_signup_started_at = _now()
    row.whatsapp_status = states.WA_META_SIGNUP_IN_PROGRESS
    _clear_whatsapp_error(row)
    db.session.commit()
    return whatsapp.embedded_signup_config(row.meta_signup_state)


def action_complete_embedded_signup(
    garage: Garage,
    *,
    state: str,
    waba_id: str,
    phone_number_id: str | None = None,
    business_id: str | None = None,
) -> GarageCommunicationsOnboarding:
    """Record what Meta handed back when the business finished signup.

    The ``state`` nonce must match the one this business's launch minted -
    otherwise a completion payload from one browser tab could attach another
    business's WABA here, which is precisely the cross-tenant mix-up the
    unique constraint on ``waba_id`` exists to catch at the database level.
    """
    row = ensure_onboarding(garage)
    if not row.meta_signup_state or state != row.meta_signup_state:
        raise ProvisioningActionError(
            "This Embedded Signup result does not belong to this business. Start signup "
            "again from this page."
        )

    clash = (
        db.session.query(GarageCommunicationsOnboarding)
        .filter(
            GarageCommunicationsOnboarding.waba_id == waba_id,
            GarageCommunicationsOnboarding.garage_id != garage.id,
        )
        .first()
    )
    if clash is not None:
        raise ProvisioningActionError(
            f"WhatsApp Business Account {waba_id} is already connected to another CoMaz "
            "business. Each business must have its own WABA."
        )

    row.waba_id = waba_id
    row.meta_phone_number_id = phone_number_id
    row.meta_business_id = business_id
    row.meta_signup_completed_at = _now()
    # Consumed: a nonce that stayed valid would let the same payload be
    # replayed after the operator moved on.
    row.meta_signup_state = None
    _clear_whatsapp_error(row)

    settings = ensure_settings(garage)
    row.whatsapp_status = (
        states.WA_TWILIO_SUBACCOUNT_READY
        if settings.twilio_subaccount_sid
        else states.WA_WABA_RECEIVED
    )
    db.session.commit()
    return row


def action_register_sender(
    garage: Garage, *, display_name: str, verification_method: str | None = None
) -> GarageCommunicationsOnboarding:
    """Register the WhatsApp sender with Twilio, binding the WABA to this
    business's subaccount."""
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)

    if not row.whatsapp_number:
        raise ProvisioningActionError("This business has no WhatsApp number recorded.")
    if not row.waba_id:
        raise ProvisioningActionError(
            "Meta has not returned a WhatsApp Business Account for this business yet."
        )
    if not settings.twilio_subaccount_sid:
        raise ProvisioningActionError(
            "Create this business's Twilio subaccount before registering its sender."
        )

    row.whatsapp_status = states.WA_SENDER_REGISTRATION_PENDING
    db.session.flush()

    try:
        snapshot = whatsapp.register_sender(
            garage,
            number_e164=row.whatsapp_number,
            waba_id=row.waba_id,
            display_name=display_name,
            verification_method=verification_method,
        )
    except (WhatsAppProvisioningError, SubaccountError) as exc:
        _fail_whatsapp(row, exc, status=states.WA_FAILED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_whatsapp_error(row)
    row.whatsapp_display_name = display_name
    _apply_sender_snapshot(garage, row, snapshot)
    db.session.commit()
    return row


def action_submit_verification_code(garage: Garage, *, code: str) -> GarageCommunicationsOnboarding:
    """Pass Meta's one-time code through to Twilio. The code is never stored."""
    row = ensure_onboarding(garage)
    if not row.whatsapp_sender_sid:
        raise ProvisioningActionError("This business has no WhatsApp sender to verify.")

    try:
        snapshot = whatsapp.submit_verification_code(garage, row.whatsapp_sender_sid, code)
    except (WhatsAppProvisioningError, SubaccountError) as exc:
        _fail_whatsapp(row, exc, status=states.WA_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_whatsapp_error(row)
    _apply_sender_snapshot(garage, row, snapshot)
    db.session.commit()
    return row


def action_refresh_whatsapp_status(garage: Garage) -> GarageCommunicationsOnboarding:
    """Ask Twilio where the sender has got to."""
    row = ensure_onboarding(garage)
    if not row.whatsapp_sender_sid:
        raise ProvisioningActionError("This business has no WhatsApp sender yet.")

    try:
        snapshot = whatsapp.fetch_sender(garage, row.whatsapp_sender_sid)
    except (WhatsAppProvisioningError, SubaccountError) as exc:
        _fail_whatsapp(row, exc, status=states.WA_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _apply_sender_snapshot(garage, row, snapshot)
    db.session.commit()
    return row


def action_reconfigure_whatsapp_webhooks(garage: Garage) -> GarageCommunicationsOnboarding:
    """Re-point a registered sender at CoMaz's WhatsApp webhooks."""
    row = ensure_onboarding(garage)
    if not row.whatsapp_sender_sid:
        raise ProvisioningActionError("This business has no WhatsApp sender yet.")
    try:
        snapshot = whatsapp.update_sender_webhooks(garage, row.whatsapp_sender_sid)
    except (WhatsAppProvisioningError, SubaccountError) as exc:
        _fail_whatsapp(row, exc, status=states.WA_ACTION_REQUIRED)
        raise ProvisioningActionError(str(exc), code=getattr(exc, "code", None)) from exc

    _clear_whatsapp_error(row)
    _apply_sender_snapshot(garage, row, snapshot)
    db.session.commit()
    return row


def _apply_sender_snapshot(
    garage: Garage, row: GarageCommunicationsOnboarding, snapshot: dict
) -> None:
    """Write a Senders v2 response onto the onboarding row, and mirror the
    sender address into ``GarageCommunicationSettings`` once it is live.

    The mirror is the whole point of the step: ``tenant_resolution.py``
    resolves an inbound WhatsApp message by matching ``To`` against
    ``whatsapp_sender``, so a sender that is ONLINE at Twilio but not mirrored
    here would have its customers' messages answered by nobody.
    """
    row.whatsapp_sender_sid = snapshot.get("sid") or row.whatsapp_sender_sid
    row.whatsapp_sender_status = snapshot.get("status") or row.whatsapp_sender_status
    row.whatsapp_display_name = snapshot.get("display_name") or row.whatsapp_display_name
    reasons = snapshot.get("offline_reasons") or []
    row.whatsapp_offline_reason = "; ".join(reasons) if reasons else None
    row.whatsapp_last_status_check_at = _now()

    row.whatsapp_status = whatsapp.state_for_sender_status(
        row.whatsapp_sender_status, current=row.whatsapp_status
    )

    if row.whatsapp_status == states.WA_ONLINE:
        settings = ensure_settings(garage)
        address = snapshot.get("sender_id") or (
            whatsapp.sender_address(row.whatsapp_number) if row.whatsapp_number else None
        )
        if address:
            settings.whatsapp_sender = address
        settings.communications_enabled = True
        if row.whatsapp_online_at is None:
            row.whatsapp_online_at = _now()


def action_test_whatsapp(garage: Garage, *, to_number: str) -> dict:
    """Send a real WhatsApp message through the ordinary send path.

    Uses ``app/communications/service.py::send_whatsapp_message`` rather than
    a provisioning-only shortcut, so a successful test proves the path
    customers will actually use - including the log row and the status
    callback that comes back to the existing webhook.
    """
    from app.communications.service import send_whatsapp_message

    row = ensure_onboarding(garage)
    log = send_whatsapp_message(
        garage=garage,
        to=to_number,
        body=(
            f"CoMaz test message for {garage.name}. If you can read this, WhatsApp is connected."
        ),
        trigger_event="PLATFORM_ADMIN_TEST",
    )

    row.whatsapp_last_test_at = _now()
    row.whatsapp_last_test_status = log.status
    if log.error_code or log.error_message:
        row.whatsapp_last_error_code = log.error_code
        row.whatsapp_last_error_message = log.error_message
    if row.whatsapp_status not in (states.WA_ONLINE, states.WA_DISABLED):
        row.whatsapp_status = states.WA_TESTING
    db.session.commit()
    return {
        "communication_log_id": str(log.id),
        "status": log.status,
        "error_code": log.error_code,
        "error_message": log.error_message,
    }


# --------------------------------------------------------------------------
# Switches
# --------------------------------------------------------------------------


def action_set_communications_enabled(
    garage: Garage, *, enabled: bool
) -> GarageCommunicationsOnboarding:
    """The master switch. Disabling stops every send for this business at
    ``app/communications/service.py`` - it does not release any Twilio
    resource, so re-enabling needs no re-provisioning."""
    row = ensure_onboarding(garage)
    settings = ensure_settings(garage)
    settings.communications_enabled = enabled

    if not enabled:
        if row.voice_status != states.VOICE_NOT_STARTED:
            row.voice_status = states.VOICE_DISABLED
        if row.whatsapp_status != states.WA_NOT_STARTED:
            row.whatsapp_status = states.WA_DISABLED
    else:
        if row.voice_status == states.VOICE_DISABLED:
            row.voice_status = _resume_voice_state(row, settings)
        if row.whatsapp_status == states.WA_DISABLED:
            row.whatsapp_status = _resume_whatsapp_state(row, settings)
    db.session.commit()
    return row


def _resume_voice_state(
    row: GarageCommunicationsOnboarding, settings: GarageCommunicationSettings
) -> str:
    """Where voice should land when a business is switched back on.

    Derived from the resources that still exist, because disabling released
    none of them - making a business redo onboarding it has already completed
    would be both wrong and infuriating.
    """
    if settings.voice_phone_number and row.voice_webhooks_configured:
        return states.VOICE_ONLINE
    if settings.voice_phone_number:
        return states.VOICE_NUMBER_ASSIGNED
    if settings.twilio_subaccount_sid:
        return states.VOICE_SUBACCOUNT_READY
    return states.VOICE_NOT_STARTED


def _resume_whatsapp_state(
    row: GarageCommunicationsOnboarding, settings: GarageCommunicationSettings
) -> str:
    """The same for WhatsApp, and for the same reason.

    Twilio's own view of the sender is the best evidence when there is one;
    below that, the furthest step whose artefact still exists. Falling back to
    NOT_STARTED whenever Twilio had said nothing would throw away a completed
    Meta Embedded Signup, which is the single most expensive step to repeat -
    it needs the business owner back on the phone.
    """
    if row.whatsapp_sender_status:
        resumed = whatsapp.state_for_sender_status(
            row.whatsapp_sender_status, current=states.WA_NOT_STARTED
        )
        if resumed != states.WA_NOT_STARTED:
            return resumed
    if row.waba_id:
        return (
            states.WA_TWILIO_SUBACCOUNT_READY
            if settings.twilio_subaccount_sid
            else states.WA_WABA_RECEIVED
        )
    if row.whatsapp_number:
        return (
            states.WA_EXISTING_MIGRATION_REQUIRED
            if row.whatsapp_number_in_use
            else states.WA_NUMBER_ENTERED
        )
    return states.WA_NOT_STARTED


def action_set_automation_enabled(garage: Garage, *, enabled: bool) -> None:
    """Turn the conversation engine on or off for this business.

    Reuses ``app/conversation/automation.py`` rather than writing the column
    directly, so the console and the existing CLI change the same thing in the
    same way.
    """
    automation.update_automation_settings(garage, conversation_automation_enabled=enabled)
    db.session.commit()


def subaccount_credential_present(garage: Garage) -> bool:
    settings = garage.communication_settings
    return has_credential(settings.twilio_subaccount_sid if settings else None)
