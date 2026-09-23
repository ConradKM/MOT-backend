"""OpenAI Realtime SIP webhook - ``POST /api/webhooks/openai/realtime``.

Configured as this project's webhook URL in the OpenAI dashboard (Settings ->
Webhooks - there is no API call that does this, see
docs/OPENAI_VOICE_SETUP.md). Handles ``realtime.call.incoming``: resolves
which CoMaz business the dialled number belongs to, accepts or rejects the
call accordingly, and - once accepted - spawns the call-control connection
(app/ai_voice/call_controller.py) that handles tool calls for the rest of
the call. No audio passes through this backend at any point.

Plain function-based route (like app/payments/webhooks.py,
app/communications/voice_webhooks.py) - OpenAI POSTs its own event envelope,
not JSON we control, and needs the *raw* request body for signature
verification.
"""

from __future__ import annotations

import gevent
from flask import current_app, request
from flask_smorest import Blueprint
from openai import InvalidWebhookSignatureError

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    DIRECTION_INBOUND,
    CommunicationLog,
)

from . import telemetry
from .call_controller import run_call_controller
from .config import openai_configured, openai_voice_enabled, openai_webhook_configured
from .instructions import build_instructions
from .openai_sip import OpenAIVoiceError, accept_call, reject_call, verify_webhook
from .tenant import caller_number_for_sip_call, resolve_business_for_sip_call, sip_header
from .tools import TOOL_SCHEMAS

openai_voice_blp = Blueprint(
    "openai_voice",
    "openai_voice",
    url_prefix="/api/webhooks/openai",
    description="OpenAI Realtime SIP webhook - inbound call routing",
)


@openai_voice_blp.route("/realtime", methods=["POST"])
def realtime_webhook():
    if not (openai_voice_enabled() and openai_configured() and openai_webhook_configured()):
        # Never a 500: a delivery arriving for a deployment that hasn't (or
        # hasn't yet) turned this on is a normal, if unexpected, occurrence -
        # mirrors app/payments/webhooks.py's identical contract.
        return {"error": "OpenAI voice is not configured for this deployment."}, 503

    payload = request.get_data()  # raw bytes - required for signature checks

    try:
        event = verify_webhook(payload, request.headers)
    except InvalidWebhookSignatureError as exc:
        # The exception message (missing header / expired timestamp / actual
        # signature mismatch) never includes the secret or payload contents -
        # only "reason=bad-signature" was logged before, which made a stale
        # secret, a clock-skewed timestamp, and a genuine mismatch
        # indistinguishable after the fact.
        current_app.logger.warning("AI_VOICE_WEBHOOK_REJECTED reason=%s", exc)
        return {"error": "Invalid signature."}, 400
    except OpenAIVoiceError:
        return {"error": "OpenAI voice is not configured for this deployment."}, 503

    if event.type != "realtime.call.incoming":
        # Safely ignore anything else (see app/ai_voice/openai_sip.py's
        # module docstring - only this one event type is ever delivered to
        # the Realtime contract's webhook today).
        current_app.logger.info("AI_VOICE_WEBHOOK_IGNORED type=%r", event.type)
        return {"received": True}, 200

    call_id = event.data.call_id
    sip_headers = event.data.sip_headers

    # Idempotency: a redelivered event for a call already logged is a no-op -
    # accepting (or rejecting) it twice would either error against OpenAI's
    # own API or, worse, spawn a second call-control connection for the same
    # call. CommunicationLog.external_id is this call's own id - no separate
    # dedup table needed.
    if CommunicationLog.query.filter_by(external_id=call_id).first() is not None:
        current_app.logger.info("AI_VOICE_WEBHOOK_DUPLICATE callSid=%s", call_id)
        return {"received": True}, 200

    garage = resolve_business_for_sip_call(sip_headers)
    caller_phone = caller_number_for_sip_call(sip_headers)
    to_header = sip_header(sip_headers, "To") or ""

    if garage is None:
        current_app.logger.warning(
            "AI_VOICE_TENANT_UNRESOLVED callSid=%s to=%r", call_id, to_header
        )
        _safe_reject(call_id)
        return {"received": True}, 200

    settings = garage.communication_settings
    if settings is None or not settings.communications_enabled:
        current_app.logger.info(
            "AI_VOICE_COMMUNICATIONS_DISABLED callSid=%s garage=%s", call_id, garage.id
        )
        _safe_reject(call_id)
        return {"received": True}, 200

    try:
        accept_call(call_id, instructions=build_instructions(garage), tools=TOOL_SCHEMAS)
    except OpenAIVoiceError:
        current_app.logger.exception(
            "AI_VOICE_ACCEPT_FAILED callSid=%s garage=%s", call_id, garage.id
        )
        _safe_reject(call_id, status_code=500)
        return {"received": True}, 200

    db.session.add(
        CommunicationLog(
            garage_id=garage.id,
            channel=CHANNEL_VOICE,
            direction=DIRECTION_INBOUND,
            external_provider="openai",
            external_id=call_id,
            from_address=caller_phone or None,
            to_address=to_header or None,
            status="accepted",
        )
    )
    db.session.commit()
    current_app.logger.info("AI_VOICE_CALL_ACCEPTED callSid=%s garage=%s", call_id, garage.id)
    telemetry.start_call(garage.id, call_id)

    _spawn_call_controller(call_id=call_id, garage=garage, caller_phone=caller_phone)

    return {"received": True}, 200


def _safe_reject(call_id: str, *, status_code: int | None = None) -> None:
    try:
        reject_call(call_id, status_code=status_code)
    except OpenAIVoiceError:
        current_app.logger.warning("AI_VOICE_REJECT_FAILED callSid=%s", call_id)


def _spawn_call_controller(*, call_id: str, garage, caller_phone: str) -> None:
    """Runs the call-control connection for the lifetime of the call, in its
    own greenlet - the webhook itself must return quickly (OpenAI's own
    retry/backoff treats a slow or non-2xx response as a delivery failure).
    """
    # current_app is a LocalProxy; unwrap it before crossing into the
    # greenlet, whose own app context needs the real Flask object, not the
    # proxy (which resolves against *this* request's context, not the
    # greenlet's). Flask's type stubs declare current_app: Flask, hiding the
    # proxy's own _get_current_object.
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    api_key = current_app.config["OPENAI_API_KEY"]
    garage_id = garage.id

    def _run() -> None:
        with app.app_context():
            from app.models.garage import Garage

            # Re-fetched inside the greenlet's own app/session context -
            # the `garage` object from the request's session must not be
            # touched from a different greenlet/session.
            call_garage = db.session.get(Garage, garage_id)
            if call_garage is None:
                return
            run_call_controller(
                api_key=api_key, call_id=call_id, garage=call_garage, caller_phone=caller_phone
            )

    gevent.spawn(_run)
