"""Inbound Twilio WhatsApp webhooks.

No conversational bot here yet - see the package-level notes in
app/communications for what's deliberately out of scope. What this does
establish for real: tenant resolution by the sender address Twilio says was
messaged, strict rejection of unsigned/unconfigured requests, best-effort
customer matching, a logged inbound-message record, and an optional,
explicitly-configured (off by default) acknowledgement reply.
"""

from flask import Response, current_app, request
from flask_smorest import Blueprint, abort
from twilio.twiml.messaging_response import MessagingResponse

from app.models.communications.communication_log import CHANNEL_WHATSAPP

from .config import is_twilio_configured
from .security import validate_twilio_request
from .service import (
    find_customer_by_phone,
    record_inbound_communication,
    update_communication_status,
)
from .tenant_resolution import resolve_garage_by_whatsapp_sender

twilio_whatsapp_blp = Blueprint(
    "twilio_whatsapp",
    "twilio_whatsapp",
    url_prefix="/api/webhooks/twilio/whatsapp",
    description="Inbound Twilio WhatsApp webhooks - message routing and status callbacks",
)


@twilio_whatsapp_blp.route("/incoming", methods=["POST"])
def incoming_whatsapp():
    """Twilio calls this for every inbound WhatsApp message to a CoMaz OS
    business sender."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
    message_sid = request.form.get("MessageSid")
    body = request.form.get("Body", "")

    garage = resolve_garage_by_whatsapp_sender(to_number)

    reply = MessagingResponse()
    if garage is None:
        current_app.logger.warning(
            "[twilio:whatsapp] incoming message to unrecognised sender %s (MessageSid=%s)",
            to_number,
            message_sid,
        )
        return Response(str(reply), mimetype="text/xml")

    customer = find_customer_by_phone(garage, from_number.removeprefix("whatsapp:"))
    record_inbound_communication(
        garage=garage,
        channel=CHANNEL_WHATSAPP,
        from_address=from_number,
        to_address=to_number,
        external_id=message_sid,
        status="received",
        body=body,
        customer=customer,
    )

    # Off by default (TWILIO_WHATSAPP_AUTO_ACK) - deliberately isolated from
    # the logging above so turning it on/off never changes what gets recorded.
    if current_app.config.get("TWILIO_WHATSAPP_AUTO_ACK"):
        reply.message(
            f"Thanks for messaging {garage.name}. This is an automated line - "
            "we'll get back to you shortly."
        )
    return Response(str(reply), mimetype="text/xml")


@twilio_whatsapp_blp.route("/status", methods=["POST"])
def whatsapp_status():
    """Twilio message status callback (queued/sent/delivered/read/failed/…)
    for a message already logged by ``/incoming`` or sent by
    app/communications/service.py::send_whatsapp_message."""
    if not is_twilio_configured():
        abort(503, message="Twilio is not configured for this deployment.")

    if not validate_twilio_request(request):
        abort(403, message="Invalid Twilio signature.")

    update_communication_status(
        external_id=request.form.get("MessageSid"),
        status=request.form.get("MessageStatus") or "unknown",
        error_code=request.form.get("ErrorCode") or None,
    )
    return ("", 204)
