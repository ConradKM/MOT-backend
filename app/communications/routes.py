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
from app.models.customer import Customer
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile

from . import queries
from .schemas import (
    CallListQueryArgsSchema,
    CallListResponseSchema,
    CommunicationLogSchema,
    ConversationListQueryArgsSchema,
    ConversationListResponseSchema,
    ConversationMessagesQueryArgsSchema,
    ConversationMessagesResponseSchema,
    InitiateCallSchema,
    MarkReadResultSchema,
    OverviewSchema,
    SendWhatsAppSchema,
    UnreadCountSchema,
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
