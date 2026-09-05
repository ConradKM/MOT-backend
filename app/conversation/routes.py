"""Development-only Conversation Simulator API (Part 29/30).

Runs a message through the *real* conversation engine - the same
engine.py::handle_message the WhatsApp webhook calls - with no Twilio
involved at all, so the full booking/cancel/reschedule/query flows can be
built and demonstrated before any Twilio credentials exist. Voice is
simulated identically: pass ``channel: "VOICE"`` with a transcript-style
``text`` (Part 30) - nothing here does speech-to-text or text-to-speech.

Gated by ``CONVERSATION_SIMULATOR_ENABLED`` (see app/config.py) - a
production deployment must set that to "false". Still requires a normal
employee JWT on top of that flag, and only ever acts on the caller's own
garage - there is no cross-tenant "simulate as any business" mode.
"""

from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.phone import InvalidPhoneNumberError, normalize_uk_mobile

from . import engine
from .schemas import ConversationResultSchema, SimulateMessageSchema

conversation_blp = Blueprint(
    "conversation",
    "conversation",
    url_prefix="/api/conversation",
    description="Development conversation simulator - runs real messages through "
    "the real booking/communications engine with no Twilio involved.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


@conversation_blp.route("/simulate")
class SimulateMessage(MethodView):

    @jwt_required()
    @conversation_blp.doc(**_AUTH_DOC)
    @conversation_blp.arguments(SimulateMessageSchema)
    @conversation_blp.response(200, ConversationResultSchema)
    def post(self, data):
        if not current_app.config.get("CONVERSATION_SIMULATOR_ENABLED", True):
            abort(404)

        garage = get_current_employee().garage

        try:
            phone_e164 = normalize_uk_mobile(data["phone"])
        except InvalidPhoneNumberError as exc:
            abort(422, message=str(exc))

        return engine.handle_message(
            garage, channel=data["channel"], phone_e164=phone_e164, text=data["text"]
        )
