"""The Realtime call-control connection - the only WebSocket this codebase
opens for OpenAI Realtime SIP calls, and it never carries audio.

Once a call is accepted (app/ai_voice/openai_sip.py::accept_call), OpenAI
handles the SIP media itself; this backend attaches a control connection
using the call's ``call_id`` purely to receive function/tool-call events and
send results back - "the WebSocket behaves exactly like any other Realtime
API connection" per https://developers.openai.com/api/docs/guides/voice-sip.

Uses the official ``openai`` SDK's synchronous ``client.realtime.connect()``
(not a hand-rolled client) - it cooperates correctly under gunicorn's gevent
worker the same way every blocking call in this codebase already does.
"""

from __future__ import annotations

import json
import logging
from time import monotonic

from openai import OpenAI

from app.models.communications.voice_call_metrics import (
    END_REASON_CONNECTION_CLOSED,
    END_REASON_CRASH,
    END_REASON_HANDOFF,
    END_REASON_HANGUP,
)

from . import telemetry
from .openai_sip import OpenAIVoiceError, hangup_call, refer_call
from .tools import CALL_ENDING_TOOLS, VoiceToolState, dispatch_tool

logger = logging.getLogger(__name__)

# A field a tool result can carry for this module's own use, never sent on
# to the model - see app/ai_voice/tools.py::_tool_request_human_handoff.
_TRANSFER_URI_FIELD = "_transfer_uri"


def run_call_controller(*, api_key: str, call_id: str, garage, caller_phone: str) -> None:
    """Blocks for the lifetime of the call. Call this from its own
    greenlet (see app/ai_voice/routes.py) - never from the webhook request
    itself, which must return quickly. ``garage``/``caller_phone`` are
    fixed for the whole call, resolved once before this is spawned - never
    re-derived from anything the model or a tool argument could claim.
    """
    client = OpenAI(api_key=api_key)
    end_after_response = False
    transfer_uri: str | None = None
    tool_state = VoiceToolState()
    # OpenAI can redeliver a completed function-call event after a transport
    # hiccup. Replaying its original output is safe; executing a mutating
    # tool again is not.
    completed_tool_outputs: dict[str, str] = {}

    try:
        with client.realtime.connect(call_id=call_id) as connection:
            while True:
                try:
                    event = connection.recv()
                except Exception:  # noqa: BLE001 - the connection closing must never crash the call
                    logger.info("AI_VOICE_CALL_CONTROL_CLOSED callSid=%s", call_id)
                    telemetry.finish_call(call_id, end_reason=END_REASON_CONNECTION_CLOSED)
                    break

                etype = getattr(event, "type", None)

                if etype == "response.function_call_arguments.done":
                    # getattr, not attribute access: `event` is a large SDK
                    # union type mypy can't narrow from the string check
                    # above, and tests exercise this with duck-typed fakes
                    # (see tests/test_ai_voice_call_controller.py) rather than
                    # real SDK event instances.
                    name = getattr(event, "name", "")
                    arguments = getattr(event, "arguments", "{}")
                    tool_call_id = getattr(event, "call_id", None)
                    cache_key = str(tool_call_id) if tool_call_id else None
                    started = monotonic()
                    if cache_key and cache_key in completed_tool_outputs:
                        output = completed_tool_outputs[cache_key]
                        logger.info(
                            "AI_VOICE_TOOL_REPLAY callSid=%s garage=%s tool=%s",
                            call_id,
                            garage.id,
                            name,
                        )
                    else:
                        logger.info(
                            "AI_VOICE_TOOL_CALL callSid=%s garage=%s tool=%s",
                            call_id,
                            garage.id,
                            name,
                        )
                        output = dispatch_tool(
                            garage,
                            caller_phone,
                            name,
                            arguments,
                            state=tool_state,
                            tool_call_id=cache_key,
                            call_id=call_id,
                        )
                        if cache_key:
                            completed_tool_outputs[cache_key] = output
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
                    telemetry.record_tool_call(
                        call_id, tool=name, outcome=outcome, latency_ms=latency_ms
                    )
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
                    model_output, transfer_uri = _extract_transfer_uri(output)
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
                        end_after_response = True

                elif etype == "response.done":
                    _record_usage_if_present(call_id, event)
                    if end_after_response:
                        logger.info(
                            "AI_VOICE_ENDING_CALL callSid=%s reason=handoff transfer=%s",
                            call_id,
                            bool(transfer_uri),
                        )
                        if transfer_uri:
                            _safe_refer(call_id, transfer_uri)
                        else:
                            _safe_hangup(call_id)
                        telemetry.finish_call(
                            call_id,
                            end_reason=END_REASON_HANDOFF if transfer_uri else END_REASON_HANGUP,
                        )
                        break

                elif etype == "error":
                    logger.warning(
                        "AI_VOICE_OPENAI_ERROR callSid=%s error=%s",
                        call_id,
                        str(getattr(event, "error", event))[:300],
                    )
    except Exception:
        logger.exception("AI_VOICE_CALL_CONTROL_CRASH callSid=%s", call_id)
        telemetry.finish_call(call_id, end_reason=END_REASON_CRASH)


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
