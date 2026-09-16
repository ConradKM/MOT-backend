"""The Twilio Media Streams <-> OpenAI Realtime bridge.

Twilio connects a WebSocket here for the duration of an inbound call and
streams JSON frames per Twilio's Media Streams protocol
(https://www.twilio.com/docs/voice/media-streams/websocket-messages):

  connected - once, first frame: just confirms the upgrade succeeded
  start     - once: streamSid, callSid, mediaFormat, customParameters
  media     - repeated: one base64-encoded audio chunk (the codec Twilio
              actually used - see mediaFormat on "start", not assumed here)
  stop      - the call ended

We open a second WebSocket to OpenAI's Realtime API (app/ai_voice/
realtime_bridge.py) and relay audio both ways essentially unchanged - no
transcoding happens in this codebase; both sides are configured to speak the
same codec (see OPENAI_REALTIME_AUDIO_FORMAT). Function/tool calls from
OpenAI are executed by app/ai_voice/tools.py, tenant-scoped to the garage
this call was resolved to.

Unlike app/ws/twilio_voice.py (ConversationRelay), Media Streams' own "start"
message carries no ``to``/``from`` fields to cross-check a client-supplied
tenant id against - so tenant safety here rests on two things instead: (1)
the WebSocket handshake's own Twilio signature (exactly the same check
twilio_voice.py's setup phase relies on as its first line of defence), and
(2) requiring the callSid to match a CommunicationLog row already created,
for that exact garage, by the /incoming webhook that built this call's TwiML
in the first place (app/communications/voice_webhooks.py) - a WebSocket
connection can never claim a garage_id that HTTP webhook didn't already log.
"""

from __future__ import annotations

import json
import time
import uuid

import gevent
from flask import current_app, request
from simple_websocket import ConnectionClosed

from app.ai_voice.config import openai_configured, realtime_ws_url
from app.ai_voice.instructions import build_instructions
from app.ai_voice.realtime_bridge import (
    EVENT_ERROR,
    EVENT_FUNCTION_CALL_DONE,
    RealtimeSession,
)
from app.ai_voice.tools import CALL_ENDING_TOOLS, TOOL_SCHEMAS, dispatch_tool
from app.ai_voice.twiml import bridge_ws_url
from app.communications.security import validate_twilio_websocket
from app.extensions import db, sock
from app.models.communications.communication_log import CommunicationLog
from app.models.garage import Garage

_HANDSHAKE_TIMEOUT_S = 10


def _log(event: str, **fields) -> None:
    parts = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in fields.items())
    current_app.logger.info("%s %s", event, parts)


def _resolve_call_garage(garage_id: str | None, call_sid: str | None) -> Garage | None:
    """The garage this call belongs to, or None if the claim doesn't hold up.
    See the module docstring for why this differs from twilio_voice.py's
    to-number cross-check."""
    if not garage_id or not call_sid:
        return None
    try:
        # Validate the shape ourselves before it ever reaches a query -
        # letting an invalid UUID hit the database raises psycopg's own
        # DataError and poisons the request-scoped session for anything
        # after it, which a bare tenant-lookup miss must never do.
        uuid.UUID(str(garage_id))
    except (ValueError, AttributeError, TypeError):
        return None
    garage = db.session.get(Garage, garage_id)
    if garage is None:
        return None
    logged = CommunicationLog.query.filter_by(garage_id=garage.id, external_id=call_sid).first()
    if logged is None:
        return None
    return garage


def _forward_openai_audio(
    twilio_ws, session: RealtimeSession, stream_sid: str, state: dict
) -> None:
    """Runs in its own greenlet for the lifetime of the call: reads OpenAI's
    server events and forwards audio/tool-calls. Never touches Twilio's own
    receive loop - the two directions are fully independent."""
    try:
        while True:
            event = session.receive()
            if event is None:
                break
            etype = event.get("type")

            if etype == "response.audio.delta":
                delta = event.get("delta")
                if delta:
                    try:
                        twilio_ws.send(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": delta},
                                }
                            )
                        )
                    except ConnectionClosed:
                        break

            elif etype == EVENT_FUNCTION_CALL_DONE:
                call_id = event.get("call_id")
                name = event.get("name") or ""
                arguments = event.get("arguments") or "{}"
                _log("AI_VOICE_TOOL_CALL", callSid=state.get("call_sid"), tool=name)
                output = dispatch_tool(state["garage"], state["caller_phone"], name, arguments)
                if call_id:
                    session.send_tool_result(call_id, output)
                if name in CALL_ENDING_TOOLS:
                    state["end_after_response"] = True

            elif etype == "response.done":
                if state.get("end_after_response"):
                    state["should_close"] = True

            elif etype == EVENT_ERROR:
                _log(
                    "AI_VOICE_OPENAI_ERROR",
                    callSid=state.get("call_sid"),
                    error=str(event.get("error"))[:300],
                )
    except Exception:
        current_app.logger.exception("AI_VOICE_OPENAI_LOOP_CRASH callSid=%s", state.get("call_sid"))
    finally:
        state["openai_loop_done"] = True


def openai_voice_bridge(ws) -> None:
    """The Media Streams WebSocket handler. Registered on ``sock`` below."""
    started = time.monotonic()
    _log(
        "AI_VOICE_WS_CONNECTED",
        remote=request.headers.get("X-Forwarded-For") or request.remote_addr or "?",
    )

    if not validate_twilio_websocket(request, bridge_ws_url()):
        _log("AI_VOICE_WS_REJECTED", reason="bad-or-missing-signature")
        return

    if not openai_configured():
        _log("AI_VOICE_WS_REJECTED", reason="openai-not-configured")
        return

    # 1. First real frame is "connected"; the "start" frame with call context
    #    follows - Twilio sends both before any "media" frame.
    stream_sid: str | None = None
    call_sid: str | None = None
    garage = None
    try:
        while stream_sid is None:
            raw = ws.receive(timeout=_HANDSHAKE_TIMEOUT_S)
            if raw is None:
                _log("AI_VOICE_SETUP_TIMEOUT", elapsed_s=round(time.monotonic() - started, 1))
                return
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if msg.get("event") == "start":
                start = msg.get("start") or {}
                call_sid = start.get("callSid")
                stream_sid = start.get("streamSid")
                garage_id = (start.get("customParameters") or {}).get("garage_id")
                garage = _resolve_call_garage(garage_id, call_sid)
    except ConnectionClosed:
        _log("AI_VOICE_WS_DISCONNECTED", phase="handshake")
        return

    if garage is None:
        _log("AI_VOICE_TENANT_REJECTED", callSid=call_sid)
        return

    _log("AI_VOICE_SETUP_RECEIVED", callSid=call_sid, garage=str(garage.id))

    # 2. Open the OpenAI side and configure the session with this business's
    #    real data + tools, before relaying any audio.
    cfg = current_app.config
    try:
        session = RealtimeSession(ws_url=realtime_ws_url(), api_key=cfg["OPENAI_API_KEY"])
        session.configure_session(
            instructions=build_instructions(garage),
            tools=TOOL_SCHEMAS,
            voice=cfg.get("OPENAI_REALTIME_VOICE", "marin"),
            audio_format=cfg.get("OPENAI_REALTIME_AUDIO_FORMAT", "g711_ulaw"),
            model=cfg.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
        )
    except Exception:
        current_app.logger.exception("AI_VOICE_OPENAI_CONNECT_FAILED callSid=%s", call_sid)
        return

    # Caller's own number isn't on Media Streams' "start" message - resolved
    # from the same CommunicationLog row _resolve_call_garage already
    # confirmed belongs to this call.
    call_log = CommunicationLog.query.filter_by(garage_id=garage.id, external_id=call_sid).first()
    caller_phone: str = (call_log.from_address or "") if call_log is not None else ""

    state = {
        "garage": garage,
        "caller_phone": caller_phone,
        "call_sid": call_sid,
        "end_after_response": False,
        "should_close": False,
        "openai_loop_done": False,
    }
    openai_greenlet = gevent.spawn(_forward_openai_audio, ws, session, stream_sid, state)

    disconnect_reason = "loop-exit"
    try:
        while True:
            if state["should_close"]:
                disconnect_reason = "ended-by-us:handoff"
                break
            try:
                raw = ws.receive(timeout=1)
            except ConnectionClosed:
                disconnect_reason = "connection-closed"
                break
            if raw is None:
                if state["openai_loop_done"]:
                    disconnect_reason = "openai-closed"
                    break
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue

            mevent = msg.get("event")
            if mevent == "media":
                payload = (msg.get("media") or {}).get("payload")
                if payload:
                    session.send_caller_audio(payload)
            elif mevent == "stop":
                disconnect_reason = "caller-hung-up"
                break
    except Exception:
        current_app.logger.exception("AI_VOICE_BRIDGE_CRASH callSid=%s", call_sid)
        disconnect_reason = "bridge-crash"
    finally:
        session.close()
        gevent.joinall([openai_greenlet], timeout=2)
        _log(
            "AI_VOICE_WS_DISCONNECTED",
            callSid=call_sid,
            reason=disconnect_reason,
            elapsed_s=round(time.monotonic() - started, 1),
        )


# Registered only when the feature is compiled in - see app/ws/__init__.py,
# which imports this module unconditionally (the route existing costs
# nothing; OPENAI_VOICE_ENABLED/openai_configured() gate whether a call is
# ever routed to it - see app/communications/voice_webhooks.py).
sock.route("/api/ws/twilio/openai-voice")(openai_voice_bridge)
