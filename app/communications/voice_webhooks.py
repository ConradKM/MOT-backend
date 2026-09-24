"""Inbound Twilio Voice webhooks.

A business with an enabled phone menu (app/communications/ivr) hears it
first; the ``/ivr/*`` routes below drive each step of that menu. Everyone
else gets exactly the pre-menu behaviour. What this also establishes: tenant
resolution by the number Twilio says was called, strict rejection of
unsigned/unconfigured requests, a logged inbound-call record, and a safe,
per-garage TwiML reply instead of anything hard-coded to one business.

Plain function-based routes (like app/health/routes.py), not the
MethodView + marshmallow style used elsewhere in the API: Twilio POSTs
form-encoded fields it defines, not JSON we control, and expects TwiML (XML)
back, not a JSON envelope - there's no OpenAPI schema worth generating here.
"""

from flask import Response, current_app, request
from flask_smorest import Blueprint, abort
from twilio.twiml.voice_response import VoiceResponse

from app.models.communications.communication_log import DIRECTION_INBOUND, CommunicationLog
from app.phone import InvalidPhoneNumberError, normalize_uk_phone

from .config import is_twilio_configured
from .events import MISSED_CALL, emit_event
from .ivr import service as ivr_service
from .ivr import twiml as ivr_twiml
from .providers.twilio import TwilioVoiceProvider
from .queries import MISSED_CALL_STATUSES
from .security import validate_twilio_request
from .service import apply_status_event, record_inbound_event
from .tenant_resolution import resolve_garage_by_voice_number
from .voice_relay import build_incoming_call_twiml, conversationrelay_enabled

twilio_voice_blp = Blueprint(
    "twilio_voice",
    "twilio_voice",
    url_prefix="/api/webhooks/twilio/voice",
    description="Inbound Twilio Voice webhooks - call routing and status callbacks",
)


@twilio_voice_blp.route("/incoming", methods=["POST"])
def incoming_call():
    """Twilio calls this the moment someone dials a CoMaz OS business number."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    event = TwilioVoiceProvider().normalise_inbound(request.form)
    to_number = event.to_address
    call_sid = event.interaction_id

    garage = resolve_garage_by_voice_number(to_number)

    reply = VoiceResponse()
    if garage is None:
        # Expected background noise (a stale/typo'd number during manual
        # Twilio console setup) rather than an error - always answer with
        # valid TwiML, never a non-2xx, so the caller hears a clean message.
        current_app.logger.warning(
            "[twilio:voice] incoming call to unrecognised number %s (CallSid=%s)",
            to_number,
            call_sid,
        )
        reply.say("Sorry, this number is not currently in service.")
        return Response(str(reply), mimetype="text/xml")

    record_inbound_event(garage, event)

    # The business's own phone menu runs before any AI session, so a caller
    # who wants a person never enters the assistant. A menu that fails to
    # build falls through to the pre-menu behaviour below, never a dropped call.
    try:
        menu_settings = ivr_service.active_menu(garage)
        if menu_settings is not None:
            return _twiml(ivr_twiml.menu(garage, menu_settings, call_sid=call_sid))
    except Exception:
        current_app.logger.exception(
            "VOICE_IVR_MENU_FAILED callSid=%r garage=%s - falling back", call_sid, garage.id
        )

    # The automated assistant, when it's switched on for this deployment. Any
    # failure building the TwiML falls through to the escalation/static
    # greeting below - a broken ConversationRelay config must never drop the
    # call.
    if conversationrelay_enabled():
        try:
            twiml = build_incoming_call_twiml(garage)
            current_app.logger.info(
                "VOICE_INCOMING callSid=%r garage=%s twiml=conversationrelay bytes=%d",
                call_sid,
                garage.id,
                len(twiml),
            )
            return Response(twiml, mimetype="text/xml")
        except Exception:
            current_app.logger.exception(
                "VOICE_INCOMING callSid=%r garage=%s twiml=build-failed - falling back to static",
                call_sid,
                garage.id,
            )

    current_app.logger.info(
        "VOICE_INCOMING callSid=%r garage=%s twiml=static conversationrelay_enabled=%s",
        call_sid,
        garage.id,
        conversationrelay_enabled(),
    )
    escalation = _escalation_number(garage)
    if escalation:
        # A business that has nominated a human destination should never hear
        # "being configured" - forward the caller instead. Set from Platform
        # Admin > Communications; see app/communications/provisioning.
        reply.say(f"Thank you for calling {garage.name}. Connecting you now.")
        reply.dial(escalation)
        return Response(str(reply), mimetype="text/xml")

    reply.say(
        f"Thank you for calling {garage.name}. "
        "Our automated booking service is currently being configured."
    )
    return Response(str(reply), mimetype="text/xml")


def _escalation_number(garage) -> str | None:
    """This business's human escalation destination, if it has set one.

    Platform-controlled configuration (Platform Admin > Communications), not
    something a garage user can write - the same boundary every other column
    on ``GarageCommunicationSettings`` sits behind.
    """
    settings = garage.communication_settings
    if settings is None:
        return None
    return settings.voice_escalation_number or settings.voice_fallback_number or None


def _twiml(response: VoiceResponse) -> Response:
    return Response(str(response), mimetype="text/xml")


def _caller_e164(raw: str) -> str:
    """The caller's number in E.164, or "" when withheld/unparseable - only
    ever used for a callback request and the signed AI handoff."""
    try:
        return normalize_uk_phone(raw)
    except InvalidPhoneNumberError:
        return raw if raw.startswith("+") and raw[1:].isdigit() else ""


def _ivr_call():
    """Shared guard for every /ivr/* step: signed by Twilio, and the tenant
    re-resolved from the number the caller dialled - never from anything in
    the query string."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")
    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")
    form = request.form
    garage = resolve_garage_by_voice_number(form.get("To", ""))
    return garage, form.get("CallSid", ""), _caller_e164(form.get("From", ""))


def _not_in_service() -> Response:
    reply = VoiceResponse()
    reply.say("Sorry, this number is not currently in service.")
    return _twiml(reply)


def _tried() -> set[str]:
    return {t for t in (request.args.get("tried") or "").split(",") if t}


def _int_arg(name: str, default: int) -> int:
    try:
        return max(0, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


@twilio_voice_blp.route("/ivr/menu", methods=["POST"])
def ivr_menu():
    """The caller pressed a key at the menu - or the Gather timed out."""
    garage, call_sid, caller = _ivr_call()
    if garage is None:
        return _not_in_service()
    settings = ivr_service.get_settings(garage.id)
    if settings is None or not settings.options:
        # The owner removed the menu mid-call: take the safe route.
        return _twiml(
            ivr_twiml.fallback(
                garage, settings, call_sid=call_sid, caller=caller, reason="menu_gone"
            )
        )
    return _twiml(
        ivr_twiml.handle_selection(
            garage,
            settings,
            call_sid=call_sid,
            caller=caller,
            digits=(request.form.get("Digits") or "").strip()[:1],
            attempt=max(1, _int_arg("attempt", 1)),
            repeat=_int_arg("repeat", 0),
        )
    )


# Statuses of the AI leg's CommunicationLog row (app/ai_voice) that mean the
# caller should now reach a person rather than be hung up on.
AI_LEG_NEEDS_PERSON = frozenset({"handoff", "ai_failed"})
_DIAL_CONNECTED = frozenset({"completed", "answered"})


@twilio_voice_blp.route("/ivr/ai-complete", methods=["POST"])
def ivr_ai_complete():
    """The AI leg ended. A normal end hangs up; an AI that failed, never
    connected, or asked for a person sends the caller to the fallback."""
    garage, call_sid, caller = _ivr_call()
    if garage is None:
        return _not_in_service()
    dial_status = request.form.get("DialCallStatus", "")
    leg = CommunicationLog.query.filter_by(
        garage_id=garage.id, external_provider="openai", call_sid=call_sid
    ).first()
    leg_status = leg.status if leg is not None else None
    current_app.logger.info(
        "VOICE_IVR_AI_COMPLETE callSid=%s garage=%s dial=%s ai=%s",
        call_sid,
        garage.id,
        dial_status,
        leg_status or "not_connected",
    )
    if leg is not None and dial_status in _DIAL_CONNECTED and leg_status not in AI_LEG_NEEDS_PERSON:
        reply = VoiceResponse()
        reply.hangup()
        return _twiml(reply)
    settings = ivr_service.get_settings(garage.id)
    reason = (
        leg_status if leg_status in AI_LEG_NEEDS_PERSON else f"ai_dial_{dial_status or 'unknown'}"
    )
    return _twiml(
        ivr_twiml.fallback(
            garage, settings, call_sid=call_sid, caller=caller, reason=reason, tried=_tried()
        )
    )


@twilio_voice_blp.route("/ivr/transfer-complete", methods=["POST"])
def ivr_transfer_complete():
    """A transfer to a person ended. Unanswered → the next safe fallback."""
    garage, call_sid, caller = _ivr_call()
    if garage is None:
        return _not_in_service()
    dial_status = request.form.get("DialCallStatus", "")
    current_app.logger.info(
        "VOICE_IVR_TRANSFER_COMPLETE callSid=%s garage=%s dial=%s", call_sid, garage.id, dial_status
    )
    if dial_status in _DIAL_CONNECTED:
        reply = VoiceResponse()
        reply.hangup()
        return _twiml(reply)
    settings = ivr_service.get_settings(garage.id)
    return _twiml(
        ivr_twiml.fallback(
            garage,
            settings,
            call_sid=call_sid,
            caller=caller,
            reason=f"transfer_{dial_status or 'unknown'}",
            tried=_tried(),
        )
    )


@twilio_voice_blp.route("/status", methods=["POST"])
def voice_status():
    """Twilio Voice status callback - call progress/completion events for a
    call already logged by ``/incoming`` (or placed by
    app/communications/service.py::initiate_voice_call)."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    event = TwilioVoiceProvider().normalise_status(request.form)
    status = event.status
    log = apply_status_event(event)
    # A call that never connected (see queries.py::MISSED_CALL_STATUSES) is
    # exactly what the MISSED_CALL automation rule exists for - only for a
    # call that came IN, never one CoMaz OS itself placed.
    if log is not None and log.direction == DIRECTION_INBOUND and status in MISSED_CALL_STATUSES:
        emit_event(MISSED_CALL, garage=log.garage, communication_log=log)
    return ("", 204)
