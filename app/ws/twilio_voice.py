"""The ConversationRelay <-> conversation-engine bridge.

Twilio's ConversationRelay connects a WebSocket here for the duration of an
inbound call and streams JSON messages
(https://www.twilio.com/docs/voice/conversationrelay/websocket-messages):

  setup    - once, on connect: callSid, from, to, customParameters, ...
  prompt   - a transcription (`voicePrompt`, `last` marks end of utterance)
  dtmf     - a keypress (`dtmf`), when dtmfDetection is on
  interrupt- the caller barged in over the assistant
  error    - a Twilio-side session error

We reply with ``{"type": "text", "token": "...", "last": true}`` for speech.

Everything about *what to say* is the existing engine
(``app/conversation/engine.py::handle_message``) - identical call to the one
``whatsapp_webhooks.py`` makes, only the transport differs. This module owns:
handshake signature validation, tenant resolution + isolation, turn framing,
and safe teardown (caller hangs up, engine raises, Twilio errors).
"""

from __future__ import annotations

import json
import time

from flask import current_app, request
from simple_websocket import ConnectionClosed

from app.communications.security import validate_twilio_websocket
from app.communications.tenant_resolution import resolve_garage_by_voice_number
from app.communications.voice_relay import bridge_ws_url
from app.conversation import actions, engine
from app.extensions import db, sock
from app.models.communications.communication_log import CommunicationLog

_HANDSHAKE_TIMEOUT_S = 10
_APOLOGY = (
    "Sorry, I'm having trouble completing that right now. I'll ask the team to call you back."
)
_HANDOFF_CLOSE = "Someone from the team will call you back shortly. Goodbye."
_NOT_AUTOMATED = "Sorry, this number is not set up for automated booking."


def _log(event: str, **fields) -> None:
    """One structured, greppable line per ConversationRelay lifecycle event
    (VOICE_WS_CONNECTED, VOICE_SETUP_RECEIVED, VOICE_PROMPT_RECEIVED,
    VOICE_RELAY_ERROR, VOICE_END_SENT, VOICE_WS_DISCONNECTED, ...). Never
    logs a full phone number or any Twilio credential."""
    parts = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in fields.items())
    current_app.logger.info("%s %s", event, parts)


def _mask_number(number: str | None) -> str:
    if not number:
        return "none"
    tail = number[-4:]
    return f"***{tail}"


def _send(ws, payload: dict) -> None:
    ws.send(json.dumps(payload))


def _speak(ws, text: str, *, last: bool = True) -> None:
    if text:
        _send(ws, {"type": "text", "token": text, "last": last})


def _run_engine(garage, phone_e164: str, text: str, external_id: str, call_sid: str | None):
    """One engine turn. Never raises - a failure becomes a spoken apology +
    a callback request, so the caller is never left with dead air."""
    try:
        return engine.handle_message(
            garage,
            channel="VOICE",
            phone_e164=phone_e164,
            text=text,
            external_message_id=external_id,
            call_sid=call_sid,
        )
    except Exception:
        current_app.logger.exception("[twilio:voice:ws] engine failure for garage %s", garage.id)
        db.session.rollback()
        _safe_callback(garage, phone_e164)
        return None


def _safe_callback(garage, phone_e164: str) -> None:
    try:
        customer = actions.find_customer(garage, phone_e164)
        actions.create_callback_request(
            garage,
            customer=customer,
            phone_e164=phone_e164,
            reason="Automated phone booking could not be completed.",
        )
    except Exception:
        current_app.logger.exception(
            "[twilio:voice:ws] could not create fallback callback for garage %s",
            garage.id,
        )
        db.session.rollback()


def _finalize_call_log(garage, call_sid: str | None, last_intent: str | None) -> None:
    """Tag the call's own CommunicationLog row (created by the /incoming
    webhook) with the detected intent. Duration / final status come from the
    Twilio voice status callback, unchanged."""
    if not call_sid:
        return
    try:
        row = (
            CommunicationLog.query.filter_by(garage_id=garage.id, external_id=call_sid)
            .order_by(CommunicationLog.created_at.asc())
            .first()
        )
        if row is not None and last_intent:
            row.intent = last_intent
            db.session.commit()
    except Exception:
        current_app.logger.exception("[twilio:voice:ws] finalize failed (CallSid=%s)", call_sid)
        db.session.rollback()


def twilio_voice_bridge(ws) -> None:
    """The ConversationRelay WebSocket handler. Registered on ``sock`` below;
    kept as a plain module-level function so it stays importable/testable
    regardless of what ``sock.route`` returns.

    Emits one structured log line per lifecycle event (grep ``VOICE_``) so a
    failed real call can be reconstructed from the platform log stream
    without a debugger."""
    started = time.monotonic()
    sig_present = "X-Twilio-Signature" in request.headers
    _log(
        "VOICE_WS_CONNECTED",
        remote=request.headers.get("X-Forwarded-For") or request.remote_addr or "?",
        ua=request.headers.get("User-Agent", "?"),
        subprotocol=request.headers.get("Sec-WebSocket-Protocol", "none"),
        sig_present=sig_present,
        validate_enabled=bool(current_app.config.get("TWILIO_WEBHOOK_VALIDATE", True)),
    )

    # 1. Handshake signature - Twilio sends X-Twilio-Signature on the upgrade
    #    request. TWILIO_WEBHOOK_VALIDATE=false skips it (tests / local).
    if not validate_twilio_websocket(request, bridge_ws_url()):
        _log("VOICE_WS_REJECTED", reason="bad-or-missing-signature", ws_url=bridge_ws_url())
        return

    # 2. First frame must be `setup`.
    call_sid: str | None = None
    try:
        raw = ws.receive(timeout=_HANDSHAKE_TIMEOUT_S)
    except ConnectionClosed:
        _log("VOICE_WS_DISCONNECTED", phase="handshake", reason="connection-closed")
        return
    if not raw:
        _log(
            "VOICE_SETUP_TIMEOUT",
            waited_s=round(time.monotonic() - started, 1),
            note="no setup frame received from Twilio",
        )
        return
    try:
        setup = json.loads(raw)
    except (ValueError, TypeError):
        _log("VOICE_SETUP_BAD_JSON", head=str(raw)[:120])
        return
    if setup.get("type") != "setup":
        _log("VOICE_SETUP_BAD_FIRST_FRAME", type=setup.get("type"))
        return

    call_sid = setup.get("callSid")
    to_number = setup.get("to") or ""
    from_number = setup.get("from") or ""
    param_garage_id = (setup.get("customParameters") or {}).get("garage_id")
    _log(
        "VOICE_SETUP_RECEIVED",
        callSid=call_sid,
        frm=_mask_number(from_number),
        to=to_number,
        direction=setup.get("direction"),
        callStatus=setup.get("callStatus"),
        params=sorted((setup.get("customParameters") or {}).keys()),
        elapsed_s=round(time.monotonic() - started, 1),
    )

    # 3. Tenant - resolve from the dialled number, cross-check the TwiML
    #    parameter. Never trust anything the connection could spoof.
    garage = resolve_garage_by_voice_number(to_number)
    if garage is None or (param_garage_id and str(garage.id) != str(param_garage_id)):
        _log(
            "VOICE_TENANT_REJECTED",
            callSid=call_sid,
            to=to_number,
            param_garage_id=param_garage_id,
            resolved=str(garage.id) if garage is not None else None,
        )
        _speak(ws, _NOT_AUTOMATED)
        return

    phone_e164 = from_number  # Twilio gives voice From in E.164 already
    turn = 0
    last_intent: str | None = None
    disconnect_reason = "loop-exit"

    try:
        while True:
            try:
                raw = ws.receive()
            except ConnectionClosed:
                disconnect_reason = "connection-closed"
                break
            if raw is None:  # caller hung up / Twilio closed the socket
                disconnect_reason = "socket-closed-by-peer"
                break
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue

            mtype = msg.get("type")
            if mtype == "prompt":
                if not msg.get("last"):
                    _log("VOICE_PROMPT_PARTIAL", callSid=call_sid, lang=msg.get("lang"))
                    continue  # partial - wait for the complete utterance
                text = (msg.get("voicePrompt") or "").strip()
                _log(
                    "VOICE_PROMPT_RECEIVED",
                    callSid=call_sid,
                    turn=turn + 1,
                    lang=msg.get("lang"),
                    last=msg.get("last"),
                    length=len(text),
                    voicePrompt=text[:160],
                )
                if not text:
                    continue
                turn += 1
                result = _run_engine(garage, phone_e164, text, f"{call_sid}:{turn}", call_sid)
                if result is None:
                    _log("VOICE_ENGINE_NULL", callSid=call_sid, turn=turn)
                    _speak(ws, _APOLOGY)
                    _send(
                        ws,
                        {
                            "type": "end",
                            "handoffData": json.dumps({"reasonCode": "automation-error"}),
                        },
                    )
                    _log("VOICE_END_SENT", callSid=call_sid, reason="automation-error")
                    disconnect_reason = "ended-by-us:automation-error"
                    break
                last_intent = result.intent
                if result.response_text:
                    _speak(ws, result.response_text)
                    _log(
                        "VOICE_REPLY_SENT",
                        callSid=call_sid,
                        turn=turn,
                        intent=result.intent,
                        length=len(result.response_text),
                    )
                elif result.needs_human:
                    _speak(ws, _HANDOFF_CLOSE)
                    _safe_callback(garage, phone_e164)
                    _send(
                        ws,
                        {
                            "type": "end",
                            "handoffData": json.dumps({"reasonCode": "human-attention-required"}),
                        },
                    )
                    _log("VOICE_END_SENT", callSid=call_sid, reason="human-attention-required")
                    disconnect_reason = "ended-by-us:human-handoff"
                    break

            elif mtype == "dtmf":
                digit = (msg.get("dtmf") or "").strip()
                _log("VOICE_DTMF_RECEIVED", callSid=call_sid, digit=digit)
                if not digit:
                    continue
                turn += 1
                result = _run_engine(garage, phone_e164, digit, f"{call_sid}:{turn}", call_sid)
                if result is None:
                    _log("VOICE_ENGINE_NULL", callSid=call_sid, turn=turn)
                    _speak(ws, _APOLOGY)
                    disconnect_reason = "ended-by-us:automation-error"
                    break
                last_intent = result.intent
                if result.response_text:
                    _speak(ws, result.response_text)

            elif mtype == "interrupt":
                _log("VOICE_INTERRUPT", callSid=call_sid)

            elif mtype == "error":
                _log(
                    "VOICE_RELAY_ERROR",
                    callSid=call_sid,
                    code=msg.get("errorCode"),
                    message=str(msg.get("errorMessage"))[:200],
                )
    except Exception:
        current_app.logger.exception("VOICE_BRIDGE_CRASH callSid=%s", call_sid)
        disconnect_reason = "bridge-crash"
        try:
            _speak(ws, _APOLOGY)
        except ConnectionClosed:
            pass
        _safe_callback(garage, phone_e164)
    finally:
        _log(
            "VOICE_WS_DISCONNECTED",
            callSid=call_sid,
            turns=turn,
            reason=disconnect_reason if call_sid else "no-setup",
            elapsed_s=round(time.monotonic() - started, 1),
        )
        _finalize_call_log(garage, call_sid, last_intent)
        # The scoped session is torn down by Flask-SQLAlchemy when this
        # request context pops - no explicit remove() here (it would also
        # detach objects mid-test).


# Register the handler on the flask-sock instance (see app/ws/__init__.py).
sock.route("/api/ws/twilio/voice")(twilio_voice_bridge)
