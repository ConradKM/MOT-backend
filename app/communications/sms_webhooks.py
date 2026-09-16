"""Inbound Twilio SMS webhooks.

Mirrors whatsapp_webhooks.py's shape exactly (tenant resolution by the
number Twilio says was messaged, strict rejection of unsigned/unconfigured
requests, best-effort customer matching, a logged inbound-message record) -
no conversation-automation dispatch here, since the conversation engine is
WhatsApp/voice-only today. An inbound SMS is simply recorded so it shows up
in Communications' SMS history; no automated reply is ever sent.
"""

from flask import Response, current_app, request
from flask_smorest import Blueprint, abort
from twilio.twiml.messaging_response import MessagingResponse

from .config import is_twilio_configured
from .providers.twilio import TwilioSMSProvider
from .security import validate_twilio_request
from .service import apply_status_event, find_customer_by_phone, record_inbound_event
from .tenant_resolution import resolve_garage_by_sms_sender

twilio_sms_blp = Blueprint(
    "twilio_sms",
    "twilio_sms",
    url_prefix="/api/webhooks/twilio/sms",
    description="Inbound Twilio SMS webhooks - message routing and status callbacks",
)


@twilio_sms_blp.route("/incoming", methods=["POST"])
def incoming_sms():
    """Twilio calls this for every inbound SMS to a CoMaz OS business
    number."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    event = TwilioSMSProvider().normalise_inbound(request.form)
    to_number = event.to_address
    from_number = event.from_address
    message_sid = event.interaction_id

    reply = MessagingResponse()

    garage = resolve_garage_by_sms_sender(to_number)
    if garage is None:
        current_app.logger.warning(
            "[twilio:sms] incoming message to unrecognised number %s (MessageSid=%s)",
            to_number,
            message_sid,
        )
        return Response(str(reply), mimetype="text/xml")

    customer = find_customer_by_phone(garage, from_number)
    record_inbound_event(garage, event, customer=customer)

    return Response(str(reply), mimetype="text/xml")


@twilio_sms_blp.route("/status", methods=["POST"])
def sms_status():
    """Twilio message status callback (queued/sent/delivered/failed/…) for a
    message already logged by ``/incoming`` or sent by
    app/communications/service.py::send_sms_message."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    apply_status_event(TwilioSMSProvider().normalise_status(request.form))
    return ("", 204)
