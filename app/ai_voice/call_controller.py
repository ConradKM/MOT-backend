"""The Realtime call-control connection - the only WebSocket this codebase
opens for OpenAI Realtime SIP calls, and it never carries audio.

Once a call is accepted (app/ai_voice/openai_sip.py::accept_call), OpenAI
handles the SIP media itself; this backend attaches a control connection
using the call's ``call_id`` purely to receive function/tool-call events and
send results back - "the WebSocket behaves exactly like any other Realtime
API connection" per https://developers.openai.com/api/docs/guides/voice-sip.

Uses the official ``openai`` SDK's synchronous ``client.realtime.connect()``
(not a hand-rolled client) - it cooperates correctly under gunicorn's gevent
worker the same way every blocking call in this codebase already does. That
method needs the ``openai[realtime]`` extra; see requirements.txt (#228).

Without this connection the model can talk but never run a tool, so a
connection that cannot be (re-)established is treated as an AI failure: the
caller is handed to the business's configured human destination rather than
left with an assistant that cannot check availability or book anything.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from time import monotonic

from openai import OpenAI

from app.extensions import db
from app.models.communications.voice_call_metrics import (
    END_REASON_CONNECTION_CLOSED,
    END_REASON_CRASH,
    END_REASON_HANDOFF,
    END_REASON_HANGUP,
)

from . import telemetry
from .openai_sip import OpenAIVoiceError, hangup_call, refer_call
from .tools import CALL_ENDING_TOOLS, VoiceToolState, _fallback_transfer_uri, dispatch_tool

logger = logging.getLogger(__name__)

# A field a tool result can carry for this module's own use, never sent on
# to the model - see app/ai_voice/tools.py::_tool_request_human_handoff.
_TRANSFER_URI_FIELD = "_transfer_uri"

# Re-establishing the control connection after an abnormal close. Tool state
# and the replay cache live outside the connection, so a reconnect can never
# repeat a booking. Kept short: a caller waiting on a tool result hears
# silence while this runs.
MAX_RECONNECT_ATTEMPTS = 2
RECONNECT_BACKOFF_SECONDS = (0.25, 0.75)

# How the call ended from the control loop's point of view.
_ENDED = "ended"  # normal close / call finished / handoff completed
_LOST = "lost"  # abnormal close - worth reconnecting


@dataclass
class _CallState:
    tool_state: VoiceToolState = field(default_factory=VoiceToolState)
    # OpenAI can redeliver a completed function-call event after a transport
    # hiccup. Replaying its original output is safe; executing a mutating
    # tool again is not.
    completed_tool_outputs: dict[str, str] = field(default_factory=dict)
    end_after_response: bool = False
    transfer_uri: str | None = None
    greeted: bool = False
    # Bridged from CoMaz's phone menu (app/communications/ivr): the Twilio
    # call is still up on the other side of this AI leg, so a handoff or
    # failure just ends the leg after marking why - the menu's Dial action
    # then transfers the caller. No SIP REFER needed.
    ivr_bridged: bool = False
    # The assistant's staff-facing reason for a handoff, kept on the AI
    # leg's row so the phone menu can attach it to a callback request if
    # nobody answers the transfer.
    handoff_reason: str | None = None


def run_call_controller(
    *,
    api_key: str,
    call_id: str,
    garage,
    caller_phone: str,
    greeting_instructions: str | None = None,
    ivr_bridged: bool = False,
) -> None:
    """Blocks for the lifetime of the call. Call this from its own
    greenlet (see app/ai_voice/routes.py) - never from the webhook request
    itself, which must return quickly. ``garage``/``caller_phone`` are
    fixed for the whole call, resolved once before this is spawned - never
    re-derived from anything the model or a tool argument could claim.

    ``greeting_instructions``, when given, is sent as the first
    ``response.create`` so the assistant speaks first instead of leaving the
    caller in silence until they say something.

    ``ivr_bridged`` marks an AI leg bridged from the business's phone menu -
    see ``_CallState.ivr_bridged``.
    """
    client = OpenAI(api_key=api_key)
    state = _CallState(ivr_bridged=ivr_bridged)
    failures = 0

    while True:
        try:
            with client.realtime.connect(call_id=call_id) as connection:
                logger.info(
                    "AI_VOICE_CONTROL_CONNECTED callSid=%s garage=%s reconnect=%d",
                    call_id,
                    garage.id,
                    failures,
                )
                failures = 0
                if greeting_instructions and not state.greeted:
                    connection.send_raw(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {"instructions": greeting_instructions},
                            }
                        )
                    )
                    state.greeted = True
                outcome = _pump(
                    connection,
                    call_id=call_id,
                    garage=garage,
                    caller_phone=caller_phone,
                    state=state,
                )
        except Exception as exc:
            logger.warning(
                "AI_VOICE_CONTROL_ERROR callSid=%s garage=%s error=%s",
                call_id,
                garage.id,
                type(exc).__name__,
            )
            logger.debug("AI_VOICE_CONTROL_ERROR detail", exc_info=True)
            outcome = _LOST

        if outcome == _ENDED:
            return

        failures += 1
        if failures > MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "AI_VOICE_CALL_CONTROL_CRASH callSid=%s garage=%s attempts=%d",
                call_id,
                garage.id,
                failures,
            )
            if state.ivr_bridged:
                _mark_ivr_leg(call_id, "ai_failed")
                logger.warning(
                    "AI_VOICE_AI_FAILED callSid=%s garage=%s fallback=phone_menu",
                    call_id,
                    garage.id,
                )
                _safe_hangup(call_id)
            else:
                _handle_ai_failure(call_id=call_id, garage=garage, caller_phone=caller_phone)
            telemetry.finish_call(call_id, end_reason=END_REASON_CRASH)
            return
        time.sleep(RECONNECT_BACKOFF_SECONDS[min(failures - 1, len(RECONNECT_BACKOFF_SECONDS) - 1)])


def _pump(connection, *, call_id: str, garage, caller_phone: str, state: _CallState) -> str:
    """Handle events until the connection closes. Returns ``_ENDED`` for a
    normal end of call and ``_LOST`` for an abnormal close worth a
    reconnect."""
    while True:
        try:
            event = connection.recv()
        except Exception as exc:  # noqa: BLE001 - the connection closing must never crash the call
            if _is_abnormal_close(exc):
                logger.warning("AI_VOICE_CALL_CONTROL_LOST callSid=%s", call_id)
                return _LOST
            logger.info("AI_VOICE_CALL_CONTROL_CLOSED callSid=%s", call_id)
            telemetry.finish_call(call_id, end_reason=END_REASON_CONNECTION_CLOSED)
            return _ENDED

        etype = getattr(event, "type", None)

        if etype == "response.function_call_arguments.done":
            _handle_function_call(
                connection,
                event,
                call_id=call_id,
                garage=garage,
                caller_phone=caller_phone,
                state=state,
            )

        elif etype == "response.done":
            _record_usage_if_present(call_id, event)
            if state.end_after_response:
                logger.info(
                    "AI_VOICE_ENDING_CALL callSid=%s reason=handoff transfer=%s",
                    call_id,
                    "phone_menu" if state.ivr_bridged else bool(state.transfer_uri),
                )
                if state.ivr_bridged:
                    _mark_ivr_leg(call_id, "handoff", detail=state.handoff_reason)
                    _safe_hangup(call_id)
                    telemetry.finish_call(call_id, end_reason=END_REASON_HANDOFF)
                    return _ENDED
                if state.transfer_uri:
                    _safe_refer(call_id, state.transfer_uri)
                else:
                    _safe_hangup(call_id)
                telemetry.finish_call(
                    call_id,
                    end_reason=END_REASON_HANDOFF if state.transfer_uri else END_REASON_HANGUP,
                )
                return _ENDED

        elif etype == "error":
            logger.warning(
                "AI_VOICE_OPENAI_ERROR callSid=%s error=%s",
                call_id,
                str(getattr(event, "error", event))[:300],
            )


def _handle_function_call(
    connection, event, *, call_id: str, garage, caller_phone: str, state: _CallState
) -> None:
    # getattr, not attribute access: `event` is a large SDK union type mypy
    # can't narrow from the string check above, and tests exercise this with
    # duck-typed fakes (see tests/test_ai_voice_call_controller.py) rather
    # than real SDK event instances.
    name = getattr(event, "name", "")
    arguments = getattr(event, "arguments", "{}")
    tool_call_id = getattr(event, "call_id", None)
    cache_key = str(tool_call_id) if tool_call_id else None
    started = monotonic()
    if cache_key and cache_key in state.completed_tool_outputs:
        output = state.completed_tool_outputs[cache_key]
        logger.info("AI_VOICE_TOOL_REPLAY callSid=%s garage=%s tool=%s", call_id, garage.id, name)
    else:
        logger.info("AI_VOICE_TOOL_CALL callSid=%s garage=%s tool=%s", call_id, garage.id, name)
        output = dispatch_tool(
            garage,
            caller_phone,
            name,
            arguments,
            state=state.tool_state,
            tool_call_id=cache_key,
            call_id=call_id,
            ivr_bridged=state.ivr_bridged,
        )
        if cache_key:
            state.completed_tool_outputs[cache_key] = output
    tool_ok = _tool_succeeded(output)
    outcome = _tool_outcome(output)
    latency_ms = int((monotonic() - started) * 1000)
    logger.info(
        "AI_VOICE_TOOL_RESULT callSid=%s garage=%s tool=%s ok=%s outcome=%s latency_ms=%d",
        call_id,
        garage.id,
        name,
        tool_ok,
        outcome,
        latency_ms,
    )
    telemetry.record_tool_call(call_id, tool=name, outcome=outcome, latency_ms=latency_ms)
    if name == "create_booking":
        logger.info(
            "AI_VOICE_BOOKING_RESULT callSid=%s garage=%s outcome=%s",
            call_id,
            garage.id,
            outcome,
        )
        telemetry.record_booking_outcome(call_id, outcome=outcome)
    if name == "request_human_handoff" and tool_ok:
        telemetry.record_escalation(call_id)
        state.handoff_reason = _handoff_reason(arguments)
    model_output, transfer_uri = _extract_transfer_uri(output)
    if transfer_uri:
        state.transfer_uri = transfer_uri
    connection.send_raw(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": model_output,
                },
            }
        )
    )
    connection.send_raw(json.dumps({"type": "response.create"}))
    if name in CALL_ENDING_TOOLS and tool_ok:
        state.end_after_response = True


def _is_abnormal_close(exc: BaseException) -> bool:
    """A close worth reconnecting: the WebSocket dropped without a clean
    close handshake, or the network failed underneath it. A normal close
    (the caller hung up, OpenAI ended the call) is not - reconnecting to a
    finished call would only fail again."""
    try:
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
    except ImportError:  # pragma: no cover - guarded by requirements (#228)
        return False
    if isinstance(exc, ConnectionClosedOK):
        return False
    return isinstance(exc, (ConnectionClosedError, OSError))


def _handle_ai_failure(*, call_id: str, garage, caller_phone: str) -> None:
    """The control connection is gone for good, so the model can no longer
    run a tool. Leave a callback request for the team and transfer the
    caller to the business's own human destination when one is configured.
    Never raises: this runs on the way out of a failed call."""
    transfer_uri = None
    try:
        db.session.rollback()
        transfer_uri = _fallback_transfer_uri(garage)
        if caller_phone:
            from app.conversation import actions

            customer = actions.find_customer(garage, caller_phone)
            actions.create_callback_request(
                garage,
                customer=customer,
                phone_e164=caller_phone,
                reason="The AI phone assistant was unavailable during this call.",
            )
    except Exception:
        logger.exception("AI_VOICE_FALLBACK_CALLBACK_FAILED callSid=%s", call_id)
        db.session.rollback()

    logger.warning(
        "AI_VOICE_AI_FAILED callSid=%s garage=%s fallback=%s",
        call_id,
        garage.id,
        "transfer" if transfer_uri else "none",
    )
    if transfer_uri:
        _safe_refer(call_id, transfer_uri)


def _handoff_reason(arguments_json: str) -> str | None:
    try:
        reason = json.loads(arguments_json or "{}").get("reason")
    except (ValueError, TypeError, AttributeError):
        return None
    text = str(reason or "").strip()[:500]
    return text or None


def _mark_ivr_leg(call_id: str, status: str, *, detail: str | None = None) -> None:
    """Record why this AI leg is ending on its CommunicationLog row, which
    the phone menu's /ivr/ai-complete step reads to decide between hanging
    up and transferring the caller. Must commit before the hangup below."""
    from app.models.communications.communication_log import CommunicationLog

    try:
        db.session.rollback()
        leg = CommunicationLog.query.filter_by(external_id=call_id).first()
        if leg is not None:
            leg.status = status
            if detail:
                leg.body = detail
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI_VOICE_LEG_MARK_FAILED callSid=%s", call_id)


def _extract_transfer_uri(output_json: str) -> tuple[str, str | None]:
    """Strips ``_transfer_uri`` (this module's own signal, never the
    model's business) out of a tool's JSON output before it's sent back to
    OpenAI, returning the cleaned JSON plus the extracted URI (if any)."""
    try:
        payload = json.loads(output_json)
    except (ValueError, TypeError):
        return output_json, None
    if not isinstance(payload, dict) or _TRANSFER_URI_FIELD not in payload:
        return output_json, None
    transfer_uri = payload.pop(_TRANSFER_URI_FIELD)
    return json.dumps(payload), transfer_uri


def _record_usage_if_present(call_id: str, event) -> None:
    """OpenAI reports token usage on the `response` object of each
    `response.done` event, not once for the whole call - see
    https://developers.openai.com/api/docs/guides/voice-sip. Duck-typed
    getattr chain, not attribute access: real SDK response objects and the
    tests' fakes (tests/test_ai_voice_call_controller.py) both just need to
    expose `.usage.input_tokens` etc., nothing more."""
    response = getattr(event, "response", None)
    usage = getattr(response, "usage", None) if response is not None else None
    if usage is None:
        return
    input_details = getattr(usage, "input_token_details", None)
    telemetry.record_usage(
        call_id,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cached_input_tokens=(
            getattr(input_details, "cached_tokens", None) if input_details is not None else None
        ),
        audio_input_seconds=getattr(usage, "audio_input_seconds", None),
        audio_output_seconds=getattr(usage, "audio_output_seconds", None),
    )


def _tool_succeeded(output_json: str) -> bool:
    try:
        payload = json.loads(output_json)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _tool_outcome(output_json: str) -> str:
    """A compact, non-PII outcome suitable for per-call production logs."""
    try:
        payload = json.loads(output_json)
    except (ValueError, TypeError):
        return "malformed_output"
    if not isinstance(payload, dict):
        return "invalid_output"
    if payload.get("ok") is not True:
        return "failed"
    return str(payload.get("status") or "succeeded")[:40]


def _safe_hangup(call_id: str) -> None:
    try:
        hangup_call(call_id)
    except OpenAIVoiceError:
        logger.warning("AI_VOICE_HANGUP_FAILED callSid=%s", call_id)


def _safe_refer(call_id: str, target_uri: str) -> None:
    try:
        refer_call(call_id, target_uri)
    except OpenAIVoiceError:
        logger.warning("AI_VOICE_REFER_FAILED callSid=%s", call_id)
        _safe_hangup(call_id)
