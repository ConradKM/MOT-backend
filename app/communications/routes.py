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

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.conversation import automation, session_service, templates
from app.conversation import queries as conversation_queries
from app.models.conversation.conversation_session import STATUS_ACTIVE, STATUS_HUMAN_HANDOFF
from app.models.customer import Customer
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile

from . import queries
from .schemas import (
    AutomationSettingsSchema,
    CallbackRequestListQueryArgsSchema,
    CallbackRequestListResponseSchema,
    CallbackRequestSchema,
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
)
from .service import find_customer_by_phone, send_whatsapp_message

communications_blp = Blueprint(
    "communications",
    "communications",
    url_prefix="/api/communications",
    description="Garage-facing calls/WhatsApp - overview, call log, WhatsApp inbox and "
    "staff-initiated contact.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


def _resolve_target_customer(garage_id, customer_id):
    if customer_id is None:
        return None
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
    if customer is None:
        abort(404, message="Customer not found.")
    return customer


def _resolve_target_phone(customer, raw_to) -> str:
    raw = customer.phone if customer is not None else raw_to
    if not raw:
        return abort(422, message="This customer has no phone number on file.")
    try:
        return normalize_uk_mobile(raw)
    except InvalidPhoneNumberError as exc:
        return abort(422, message=str(exc))


def _normalize_path_phone(phone: str) -> str:
    try:
        return normalize_uk_mobile(phone)
    except InvalidPhoneNumberError as exc:
        return abort(422, message=str(exc))


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
    @communications_blp.response(200, CommunicationLogSchema)
    def get(self, call_id):
        garage = get_current_employee().garage
        call = queries.get_call_detail(garage, call_id)
        if call is None:
            abort(404, message="Call not found.")
        return call


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
