"""Garage-facing Communications API - authenticated, tenant-scoped reads and
actions on top of app/communications/{queries,service}.py.

Separate from voice_webhooks.py / whatsapp_webhooks.py, which are Twilio's
own unauthenticated callback surface - nothing here is reachable without a
valid employee JWT, and every query is scoped to that employee's own garage.
Available to any authenticated employee (not owner-only): communicating with
customers is a normal staff task, not garage administration. The
platform-controlled Twilio configuration itself stays owner/CLI-only exactly
as before (app/garages/details.py, app/communications/cli.py) - nothing here
lets a garage user see or change it.
"""

from __future__ import annotations

from flask import Response, current_app, request
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from twilio.twiml.voice_response import VoiceResponse

from app.auth.utils import get_current_employee
from app.conversation import automation, session_service, templates
from app.conversation import queries as conversation_queries
from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)
from app.models.conversation.conversation_session import STATUS_ACTIVE, STATUS_HUMAN_HANDOFF
from app.models.customer import Customer
from app.models.employee import Employee
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile, normalize_uk_phone

from . import queries
from .schemas import (
    AttentionQueueResponseSchema,
    AutomationSettingsSchema,
    CallbackRequestListQueryArgsSchema,
    CallbackRequestListResponseSchema,
    CallbackRequestSchema,
    CallDetailSchema,
    CallListQueryArgsSchema,
    CallListResponseSchema,
    CommunicationLogSchema,
    ConversationAutomationStatusSchema,
    ConversationListQueryArgsSchema,
    ConversationListResponseSchema,
    ConversationMessagesQueryArgsSchema,
    ConversationMessagesResponseSchema,
    InitiateCallSchema,
    MarkReadResultSchema,
    MessageTemplateListResponseSchema,
    MessageTemplateSchema,
    OverviewSchema,
    SendWhatsAppSchema,
    TemplatePreviewResultSchema,
    TemplatePreviewSchema,
    UnreadCountSchema,
    UpdateMessageTemplateSchema,
    VoiceTokenSchema,
)
from .security import validate_twilio_request
from .service import find_customer_by_phone, send_whatsapp_message
from .voice_calling import (
    browser_calling_configured,
    build_voice_access_token,
    parse_client_identity,
)

communications_blp = Blueprint(
    "communications",
    "communications",
    url_prefix="/api/communications",
    description="Business-facing calls/WhatsApp - overview, call log, WhatsApp inbox and "
    "staff-initiated contact.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _resolve_target_customer(garage_id, customer_id):
    if customer_id is None:
        return None
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
    if customer is None:
        abort(404, message="Customer not found.")
    return customer


def _resolve_target_phone(customer, raw_to) -> str:
    # flask_smorest.abort always raises (it's typed Any, since flask_smorest
    # has no stubs - see the mypy override in pyproject.toml), so these never
    # actually return.
    raw = customer.phone if customer is not None else raw_to
    if not raw:
        return abort(422, message="This customer has no phone number on file.")  # type: ignore[no-any-return]
    try:
        return normalize_uk_mobile(raw)
    except InvalidPhoneNumberError as exc:
        return abort(422, message=str(exc))  # type: ignore[no-any-return]


def _normalize_path_phone(phone: str) -> str:
    try:
        return normalize_uk_mobile(phone)
    except InvalidPhoneNumberError as exc:
        return abort(422, message=str(exc))  # type: ignore[no-any-return]


@communications_blp.route("/overview")
class Overview(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, OverviewSchema)
    def get(self):
        garage = get_current_employee().garage
        return queries.overview_summary(garage)


@communications_blp.route("/unread-count")
class UnreadCount(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, UnreadCountSchema)
    def get(self):
        garage = get_current_employee().garage
        return {"whatsapp_unread": queries.unread_whatsapp_count(garage)}


@communications_blp.route("/calls")
class CallList(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(CallListQueryArgsSchema, location="query")
    @communications_blp.response(200, CallListResponseSchema)
    def get(self, args):
        garage = get_current_employee().garage
        items, total = queries.list_calls(
            garage,
            direction=args["direction"],
            missed_only=args["missed_only"],
            search=args["search"],
            limit=args["limit"],
            offset=args["offset"],
        )
        return {"items": items, "total": total}

    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(InitiateCallSchema)
    def post(self, data):
        """The real seam for staff-initiated outbound calling - not wired to
        actually place one yet.

        Twilio can already dial a number and play TwiML
        (app/communications/service.py::initiate_voice_call), but there is no
        agent/browser-calling bridge to connect the staff member who clicked
        "Call" to that live call (see docs/TWILIO_SETUP.md). Actually placing
        one here would just ring the customer to nobody, which is worse than
        being honest that this isn't ready. The frontend disables the call
        UI using GET /overview's capabilities.outbound_calling_supported;
        this 501 is the defensive backstop if it's ever reached anyway.
        """
        garage = get_current_employee().garage
        _resolve_target_customer(garage.id, data["customer_id"])  # 404s an invalid id
        return abort(501, message="Phone services are not connected yet.")


@communications_blp.route("/calls/<uuid:call_id>")
class CallDetail(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, CallDetailSchema)
    def get(self, call_id):
        garage = get_current_employee().garage
        call = queries.get_call_detail(garage, call_id)
        if call is None:
            abort(404, message="Call not found.")
        call.transcript = queries.get_call_transcript(garage, call)
        return call


# --------------------------------------------------------------------------
# Browser (Twilio Voice SDK) outbound calling - see
# app/communications/voice_calling.py. Separate from the inbound
# ConversationRelay assistant; shares only the Twilio account + call log.
# --------------------------------------------------------------------------


@communications_blp.route("/voice/token")
class VoiceToken(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, VoiceTokenSchema)
    def get(self):
        """A short-lived Voice Access Token for this staff member to place
        browser calls for their own business. Never returns the Auth Token,
        the API Key secret, or any permanent credential."""
        employee = get_current_employee()
        if not browser_calling_configured():
            abort(503, message="Browser calling is not configured for this deployment.")
        settings = employee.garage.communication_settings
        if not (settings and settings.voice_phone_number):
            abort(409, message="This business has no outbound voice number configured.")
        token, identity = build_voice_access_token(employee.garage, employee)
        return {
            "token": token,
            "identity": identity,
            "expires_in": int(current_app.config.get("TWILIO_VOICE_TOKEN_TTL", 3600)),
            "caller_id": settings.voice_phone_number,
        }


@communications_blp.route("/voice/outbound", methods=["POST"])
@communications_blp.doc(hide=True)
def voice_outbound():
    """TwiML Application Voice URL for the browser dialler. Twilio POSTs the
    dialled ``To``, the ``client:<identity>`` ``From`` and the parent
    ``CallSid``; we reply with a ``<Dial>`` from the business's own number
    and log the call once. The caller ID is set here, server-side - the
    browser can never choose it, so it can't spoof another number - and the
    garage is re-derived from the signed identity, so a call can't cross
    tenants. The TwiML App's Status Callback (pointed at
    ``/api/webhooks/twilio/voice/status``) fills in status + duration."""
    if not validate_twilio_request(request):
        return Response("<Response><Reject/></Response>", mimetype="text/xml", status=403)

    ids = parse_client_identity(request.form.get("From", ""))
    reply = VoiceResponse()
    if ids is None:
        reply.say("Sorry, this call could not be placed.")
        return Response(str(reply), mimetype="text/xml")

    garage_id, employee_id = ids
    employee = Employee.query.filter_by(id=employee_id, garage_id=garage_id).first()
    if employee is None:
        reply.say("Sorry, this call could not be placed.")
        return Response(str(reply), mimetype="text/xml")

    garage = employee.garage
    settings = garage.communication_settings
    if not (settings and settings.voice_phone_number):
        reply.say("This business is not set up for outbound calling.")
        return Response(str(reply), mimetype="text/xml")

    try:
        to_e164 = normalize_uk_phone(request.form.get("To", ""))
    except InvalidPhoneNumberError:
        reply.say("Sorry, that number is not valid.")
        return Response(str(reply), mimetype="text/xml")

    call_sid = request.form.get("CallSid")
    if call_sid and CommunicationLog.query.filter_by(external_id=call_sid).first() is None:
        customer = find_customer_by_phone(garage, to_e164)
        db.session.add(
            CommunicationLog(
                garage_id=garage.id,
                customer_id=customer.id if customer else None,
                initiated_by_employee_id=employee.id,
                channel=CHANNEL_VOICE,
                direction=DIRECTION_OUTBOUND,
                external_provider="twilio",
                external_id=call_sid,
                call_sid=call_sid,
                from_address=settings.voice_phone_number,
                to_address=to_e164,
                status=request.form.get("CallStatus") or "initiated",
            )
        )
        db.session.commit()

    dial = reply.dial(caller_id=settings.voice_phone_number, answer_on_bridge=True)
    dial.number(to_e164)
    return Response(str(reply), mimetype="text/xml")


@communications_blp.route("/conversations")
class ConversationList(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(ConversationListQueryArgsSchema, location="query")
    @communications_blp.response(200, ConversationListResponseSchema)
    def get(self, args):
        garage = get_current_employee().garage
        items, total = queries.list_conversations(
            garage, search=args["search"], limit=args["limit"], offset=args["offset"]
        )
        return {"items": items, "total": total}


@communications_blp.route("/conversations/<string:phone>/messages")
class ConversationMessages(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(ConversationMessagesQueryArgsSchema, location="query")
    @communications_blp.response(200, ConversationMessagesResponseSchema)
    def get(self, args, phone):
        garage = get_current_employee().garage
        phone_e164 = _normalize_path_phone(phone)

        messages = queries.get_conversation_messages(garage, phone_e164, limit=args["limit"])
        customer = find_customer_by_phone(garage, phone_e164)
        return {"phone": phone_e164, "customer": customer, "messages": messages}


@communications_blp.route("/conversations/<string:phone>/read")
class ConversationRead(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, MarkReadResultSchema)
    def post(self, phone):
        garage = get_current_employee().garage
        phone_e164 = _normalize_path_phone(phone)
        return {"updated": queries.mark_conversation_read(garage, phone_e164)}


@communications_blp.route("/whatsapp/send")
class WhatsAppSend(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(SendWhatsAppSchema)
    @communications_blp.response(200, CommunicationLogSchema)
    def post(self, data):
        garage = get_current_employee().garage
        customer = _resolve_target_customer(garage.id, data["customer_id"])
        to = _resolve_target_phone(customer, data["to"])
        if customer is None:
            customer = find_customer_by_phone(garage, to)

        return send_whatsapp_message(garage=garage, to=to, body=data["body"], customer=customer)


# --------------------------------------------------------------------------
# Communications automation settings (Part 26/40) - owner-editable toggles
# only; Twilio credentials/resource ids stay platform/CLI-only exactly as
# before (app/garages/details.py, app/communications/cli.py) - nothing here
# reads or writes them.
# --------------------------------------------------------------------------


@communications_blp.route("/automation-settings")
class AutomationSettings(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, AutomationSettingsSchema)
    def get(self):
        garage = get_current_employee().garage
        return automation.get_automation_settings(garage)

    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(AutomationSettingsSchema)
    @communications_blp.response(200, AutomationSettingsSchema)
    def put(self, data):
        garage = get_current_employee().garage
        return automation.update_automation_settings(garage, **data)


# --------------------------------------------------------------------------
# Message templates (Part 24/34) - safe {{variable}} text only, enforced by
# app/conversation/templates.py; there is no way to submit anything here that
# executes as code.
# --------------------------------------------------------------------------


def _template_payload(garage, key: str) -> dict:
    body, is_custom = templates.get_template_body(garage, key)
    return {
        "key": key,
        "body": body,
        "default_body": templates.DEFAULT_TEMPLATES.get(key, ""),
        "is_custom": is_custom,
    }


def _require_known_template_key(key: str) -> None:
    if key not in templates.TEMPLATE_KEYS:
        abort(404, message="Unknown template key.")


@communications_blp.route("/templates")
class MessageTemplateList(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, MessageTemplateListResponseSchema)
    def get(self):
        garage = get_current_employee().garage
        return {"items": [_template_payload(garage, key) for key in templates.TEMPLATE_KEYS]}


@communications_blp.route("/templates/<string:key>")
class MessageTemplateDetail(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(UpdateMessageTemplateSchema)
    @communications_blp.response(200, MessageTemplateSchema)
    def put(self, data, key):
        garage = get_current_employee().garage
        _require_known_template_key(key)
        templates.set_template_body(garage, key, data["body"])
        return _template_payload(garage, key)

    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, MessageTemplateSchema)
    def delete(self, key):
        """Resets this key back to the CoMaz OS default wording."""
        garage = get_current_employee().garage
        _require_known_template_key(key)
        templates.reset_template_body(garage, key)
        return _template_payload(garage, key)


@communications_blp.route("/templates/<string:key>/preview")
class MessageTemplatePreview(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(TemplatePreviewSchema)
    @communications_blp.response(200, TemplatePreviewResultSchema)
    def post(self, data, key):
        """Renders whatever draft text the owner is currently typing, with
        representative sample values - never saved, never sent."""
        _require_known_template_key(key)
        return {"preview": templates.preview_body(data["body"])}


# --------------------------------------------------------------------------
# Callback requests (Part 21/38) - created by the conversation engine's
# CALLBACK_REQUEST intent (app/conversation/actions.py::create_callback_request);
# this is the staff-facing side of the same table.
# --------------------------------------------------------------------------


def _resolve_callback_request(garage, callback_id):
    callback = conversation_queries.get_callback_request(garage, callback_id)
    if callback is None:
        abort(404, message="Callback request not found.")
    return callback


@communications_blp.route("/callback-requests")
class CallbackRequestList(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.arguments(CallbackRequestListQueryArgsSchema, location="query")
    @communications_blp.response(200, CallbackRequestListResponseSchema)
    def get(self, args):
        garage = get_current_employee().garage
        items, total = conversation_queries.list_callback_requests(
            garage, status=args["status"], limit=args["limit"], offset=args["offset"]
        )
        return {"items": items, "total": total}


@communications_blp.route("/callback-requests/<uuid:callback_id>/complete")
class CallbackRequestComplete(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, CallbackRequestSchema)
    def post(self, callback_id):
        garage = get_current_employee().garage
        callback = _resolve_callback_request(garage, callback_id)
        conversation_queries.complete_callback_request(callback)
        return callback


@communications_blp.route("/callback-requests/<uuid:callback_id>/cancel")
class CallbackRequestCancel(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, CallbackRequestSchema)
    def post(self, callback_id):
        garage = get_current_employee().garage
        callback = _resolve_callback_request(garage, callback_id)
        conversation_queries.cancel_callback_request(callback)
        return callback


# --------------------------------------------------------------------------
# Staff conversation takeover / resume automation (Part 20) - the bot and a
# human must never reply at the same time, so these simply drive the same
# ConversationSession state the engine itself reads before every reply.
# --------------------------------------------------------------------------


def _automation_status_payload(phone_e164: str, session) -> dict:
    if session is None:
        return {"phone": phone_e164, "status": None, "intent": None, "handoff_reason": None}
    return {
        "phone": phone_e164,
        "status": session.status,
        "intent": session.intent,
        "handoff_reason": session.handoff_reason,
    }


@communications_blp.route("/attention-queue")
class AttentionQueue(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, AttentionQueueResponseSchema)
    def get(self):
        """Every WhatsApp conversation automation could not resolve and
        handed to a human - the queue a staff member works through, rather
        than hunting for it inside the full conversation list."""
        garage = get_current_employee().garage
        return {"items": conversation_queries.list_handoff_sessions(garage)}


@communications_blp.route("/conversations/<string:phone>/automation")
class ConversationAutomationStatus(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, ConversationAutomationStatusSchema)
    def get(self, phone):
        garage = get_current_employee().garage
        phone_e164 = _normalize_path_phone(phone)
        session = conversation_queries.get_conversation_session(garage, phone_e164)
        return _automation_status_payload(phone_e164, session)


@communications_blp.route("/conversations/<string:phone>/takeover")
class ConversationTakeover(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, ConversationAutomationStatusSchema)
    def post(self, phone):
        garage = get_current_employee().garage
        phone_e164 = _normalize_path_phone(phone)
        session = conversation_queries.get_conversation_session(garage, phone_e164)
        if session is None or session.status not in (STATUS_ACTIVE, STATUS_HUMAN_HANDOFF):
            abort(404, message="No live automated conversation to take over for this number.")

        employee = get_current_employee()
        session_service.handoff_to_human(
            session, f"Taken over by {employee.first_name} {employee.last_name}."
        )
        return _automation_status_payload(phone_e164, session)


@communications_blp.route("/conversations/<string:phone>/resume-automation")
class ConversationResumeAutomation(MethodView):
    @jwt_required()
    @communications_blp.doc(**_AUTH_DOC)
    @communications_blp.response(200, ConversationAutomationStatusSchema)
    def post(self, phone):
        garage = get_current_employee().garage
        phone_e164 = _normalize_path_phone(phone)
        session = conversation_queries.get_conversation_session(garage, phone_e164)
        if session is None or session.status != STATUS_HUMAN_HANDOFF:
            abort(404, message="No conversation is currently handed off for this number.")

        session_service.resume_automation(session)
        return _automation_status_payload(phone_e164, session)
