"""Inbound Twilio Voice webhooks.

No AI/IVR here yet - see the package-level notes in app/communications for
what's deliberately out of scope. What this does establish for real: tenant
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

from app.models.communications.communication_log import CHANNEL_VOICE

from .config import is_twilio_configured
from .security import validate_twilio_request
from .service import record_inbound_communication, update_communication_status
from .tenant_resolution import resolve_garage_by_voice_number

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

    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
    call_sid = request.form.get("CallSid")

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

    record_inbound_communication(
        garage=garage,
        channel=CHANNEL_VOICE,
        from_address=from_number,
        to_address=to_number,
        external_id=call_sid,
        status=request.form.get("CallStatus") or "received",
    )

    reply.say(
        f"Thank you for calling {garage.name}. "
        "Our automated booking service is currently being configured."
    )
    return Response(str(reply), mimetype="text/xml")


@twilio_voice_blp.route("/status", methods=["POST"])
def voice_status():
    """Twilio Voice status callback - call progress/completion events for a
    call already logged by ``/incoming`` (or placed by
    app/communications/service.py::initiate_voice_call)."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    duration = request.form.get("CallDuration")
    update_communication_status(
        external_id=request.form.get("CallSid"),
        status=request.form.get("CallStatus") or "unknown",
        call_duration_seconds=int(duration) if duration and duration.isdigit() else None,
        error_code=request.form.get("ErrorCode") or None,
    )
    return ("", 204)
