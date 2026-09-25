"""TwiML for each step of an inbound call that goes through a phone menu.

Every function returns a complete ``VoiceResponse``. The invariant this module
exists to keep: **no path ends the call by accident.** Invalid input, no
input, a selected action that is unavailable, an AI leg that fails or hands
off, and a transfer nobody answers all resolve - deterministically - to the
business's configured fallback, and if even that is impossible, to a spoken
close after logging a callback request. Nothing here reads caller speech.

Logs use ``VOICE_IVR_*`` markers with the tenant, CallSid and outcome only -
never the caller's number or anything they said.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from flask import current_app
from twilio.twiml.voice_response import Gather, VoiceResponse

from app.ai_voice import sip_handoff

from .. import telephony
from . import actions, service

logger = logging.getLogger(__name__)

WEBHOOK_BASE = "/api/webhooks/twilio/voice/ivr"
GATHER_TIMEOUT_SECONDS = 6
# Kept for callers that only need the default; the live value is per
# business (telephony.transfer_timeout).
TRANSFER_TIMEOUT_SECONDS = telephony.DEFAULT_TRANSFER_TIMEOUT_SECONDS
# REPEAT_MENU presses are not failed attempts, but they must still end.
MAX_REPEATS = 3
# A callback request this recent, for this caller, already covers this call -
# a failed transfer after the assistant logged one must not log a second.
CALLBACK_DEDUP_WINDOW = timedelta(minutes=30)

CLOSING_LINE = (
    "Sorry, we can't take your call right now. We've noted your number and "
    "the team will call you back as soon as possible. Goodbye."
)


def _url(step: str, **params) -> str:
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{base}{WEBHOOK_BASE}/{step}" + (f"?{query}" if query else "")


def _say(node, text: str) -> None:
    cfg = current_app.config
    node.say(
        text,
        voice=cfg.get("IVR_TTS_VOICE") or "Polly.Amy-Neural",
        language=cfg.get("IVR_TTS_LANGUAGE") or "en-GB",
    )


def _option_prompt(option: dict) -> str:
    if option.get("prompt"):
        return str(option["prompt"])
    return f"For {option['label']}, press {option['digit']}."


def _gather(
    response: VoiceResponse, settings, *, attempt: int, repeat: int, lead: str | None
) -> None:
    gather: Gather = response.gather(
        input="dtmf",
        num_digits=1,
        timeout=GATHER_TIMEOUT_SECONDS,
        action=_url("menu", attempt=attempt, repeat=repeat),
        method="POST",
        action_on_empty_result=True,
    )
    parts = [lead] if lead else []
    parts.extend(_option_prompt(o) for o in settings.options)
    _say(gather, " ".join(parts))


def menu(garage, settings, *, call_sid: str | None) -> VoiceResponse:
    """The first thing a caller hears: greeting + options."""
    response = VoiceResponse()
    greeting = settings.greeting or f"Thanks for calling {garage.name}."
    logger.info(
        "VOICE_IVR_MENU callSid=%s garage=%s options=%d", call_sid, garage.id, len(settings.options)
    )
    _gather(response, settings, attempt=1, repeat=0, lead=greeting)
    return response


def handle_selection(
    garage, settings, *, call_sid: str, caller: str, digits: str, attempt: int, repeat: int
) -> VoiceResponse:
    """The caller pressed something (or nothing) at the menu."""
    response = VoiceResponse()
    option = service.option_for_digit(settings, digits) if digits else None

    if option is None:
        reason = "no_input" if not digits else "invalid"
        logger.info(
            "VOICE_IVR_%s callSid=%s garage=%s attempt=%d",
            reason.upper(),
            call_sid,
            garage.id,
            attempt,
        )
        if attempt < settings.max_attempts:
            lead = (
                "Sorry, I didn't catch that."
                if reason == "no_input"
                else "Sorry, that isn't one of the options."
            )
            _gather(response, settings, attempt=attempt + 1, repeat=repeat, lead=lead)
            return response
        return fallback(
            garage, settings, call_sid=call_sid, caller=caller, reason=f"{reason}_exhausted"
        )

    logger.info(
        "VOICE_IVR_SELECTED callSid=%s garage=%s digit=%s action=%s",
        call_sid,
        garage.id,
        option["digit"],
        option["action"],
    )
    if option["action"] == actions.REPEAT_MENU:
        if repeat >= MAX_REPEATS:
            return fallback(
                garage, settings, call_sid=call_sid, caller=caller, reason="repeat_exhausted"
            )
        _gather(response, settings, attempt=attempt, repeat=repeat + 1, lead=None)
        return response

    if not run_action(
        response,
        garage,
        settings,
        option["action"],
        option=option,
        call_sid=call_sid,
        caller=caller,
    ):
        return fallback(
            garage,
            settings,
            call_sid=call_sid,
            caller=caller,
            reason="action_unavailable",
            exclude={option["action"]},
        )
    return response


def run_action(
    response: VoiceResponse,
    garage,
    settings,
    action: str,
    *,
    option: dict | None,
    call_sid: str,
    caller: str,
    tried: set[str] | None = None,
    human_index: int = 0,
) -> bool:
    """Append the TwiML for ``action`` to ``response``. False when the
    action can't run right now (so the caller falls back instead).

    ``human_index`` is which entry of the business's human destination
    chain (telephony.human_destinations) a HUMAN_TRANSFER dials - 0 for the
    first attempt, then whatever /ivr/transfer-complete says is next."""
    tried = tried or set()
    if action in actions.AI_ACTIONS:
        if not actions.ai_voice_available():
            logger.warning("VOICE_IVR_AI_UNAVAILABLE callSid=%s garage=%s", call_sid, garage.id)
            return False
        try:
            uri = sip_handoff.openai_sip_uri(
                garage_id=str(garage.id), twilio_call_sid=call_sid, route=action, caller=caller
            )
        except ValueError:
            logger.warning("VOICE_IVR_AI_UNAVAILABLE callSid=%s garage=%s", call_sid, garage.id)
            return False
        logger.info("VOICE_IVR_AI_DIAL callSid=%s garage=%s route=%s", call_sid, garage.id, action)
        dial = response.dial(
            action=_url("ai-complete", route=action, tried=",".join(sorted(tried | {"AI"}))),
            method="POST",
        )
        dial.sip(uri)
        return True

    if action == actions.HUMAN_TRANSFER:
        return dial_human(
            response,
            garage,
            settings,
            option=option,
            call_sid=call_sid,
            caller=caller,
            tried=tried,
            index=human_index,
        )

    return False


def dial_human(
    response: VoiceResponse,
    garage,
    settings,
    *,
    option: dict | None,
    call_sid: str,
    caller: str,
    tried: set[str],
    index: int,
) -> bool:
    """Ring entry ``index`` of the business's human destination chain.

    The ``<Dial>`` reports back to /ivr/transfer-complete with the next
    index, so an unanswered, busy or failed attempt moves on to the next
    destination - and after the last one, to a logged callback. The chain is
    finite (telephony.MAX_HUMAN_DESTINATIONS) and each index is tried once.
    Nothing says the caller is connected until Twilio reports it."""
    chain = telephony.human_destinations(garage, settings, option)
    if index >= len(chain):
        if index == 0:
            logger.warning("VOICE_IVR_NO_TRANSFER_TARGET callSid=%s garage=%s", call_sid, garage.id)
        return False
    destination = chain[index]
    logger.info(
        "VOICE_IVR_TRANSFER callSid=%s garage=%s attempt=%d of=%d kind=%s source=%s",
        call_sid,
        garage.id,
        index + 1,
        len(chain),
        destination.kind,
        destination.source,
    )
    _say(
        response,
        "Putting you through now."
        if index == 0
        else "Sorry for the wait. Trying someone else now.",
    )
    dial = response.dial(
        action=_url(
            "transfer-complete",
            tried=",".join(sorted(tried | {"HUMAN"})),
            hi=index + 1,
            od=(option or {}).get("digit"),
        ),
        method="POST",
        timeout=telephony.transfer_timeout(garage),
    )
    if destination.kind == telephony.DEST_SIP_URI:
        dial.sip(destination.value)
    else:
        byoc = telephony.pstn_byoc_trunk(garage)
        if byoc:
            dial.number(destination.value, byoc=byoc)
        else:
            dial.number(destination.value)
    telephony.record_transfer_attempt(
        garage, call_sid=call_sid, caller=caller, destination=destination, index=index
    )
    return True


def fallback(
    garage,
    settings,
    *,
    call_sid: str,
    caller: str,
    reason: str,
    exclude: set[str] | None = None,
    tried: set[str] | None = None,
    detail: str | None = None,
) -> VoiceResponse:
    """The configured safe fallback, then a person, then a logged callback -
    in that order, skipping anything already tried on this call."""
    exclude = set(exclude or ())
    tried = set(tried or ())
    if "AI" in tried:
        exclude |= actions.AI_ACTIONS
    if "HUMAN" in tried:
        exclude.add(actions.HUMAN_TRANSFER)

    candidates = []
    configured = settings.fallback_action if settings else actions.HUMAN_TRANSFER
    if configured in actions.FALLBACK_ACTIONS:
        candidates.append(configured)
    if actions.HUMAN_TRANSFER not in candidates:
        candidates.append(actions.HUMAN_TRANSFER)

    response = VoiceResponse()
    for action in candidates:
        if action in exclude:
            continue
        if run_action(
            response,
            garage,
            settings,
            action,
            option=None,
            call_sid=call_sid,
            caller=caller,
            tried=tried,
        ):
            logger.info(
                "VOICE_IVR_FALLBACK callSid=%s garage=%s reason=%s action=%s",
                call_sid,
                garage.id,
                reason,
                action,
            )
            return response

    return close_with_callback(
        garage, call_sid=call_sid, caller=caller, reason=reason, detail=detail
    )


def _recent_open_callback(garage, caller: str):
    from app.models.conversation.callback_request import STATUS_PENDING, CallbackRequest

    since = datetime.now(UTC) - CALLBACK_DEDUP_WINDOW
    return (
        CallbackRequest.query.filter(
            CallbackRequest.garage_id == garage.id,
            CallbackRequest.phone_number == caller,
            CallbackRequest.status == STATUS_PENDING,
            CallbackRequest.created_at >= since,
        )
        .order_by(CallbackRequest.created_at.desc())
        .first()
    )


def close_with_callback(
    garage, *, call_sid: str, caller: str, reason: str, detail: str | None = None
) -> VoiceResponse:
    """Nothing could take the call: leave the team a callback request and
    tell the caller so, rather than dropping them.

    At most one open callback per caller per call: one logged in the last
    few minutes already covers this. ``detail`` is the assistant's own
    staff-facing summary of what the caller needed, when there is one."""
    logged = False
    if caller:
        try:
            from app.conversation import actions as conversation_actions

            if _recent_open_callback(garage, caller) is not None:
                logged = True
            else:
                text = "Phone menu: the caller couldn't be connected to the team or assistant."
                if detail:
                    text = f"{text} They needed: {detail}"[:1000]
                conversation_actions.create_callback_request(
                    garage,
                    customer=conversation_actions.find_customer(garage, caller),
                    phone_e164=caller,
                    reason=text,
                )
                logged = True
        except Exception:
            from app.extensions import db

            db.session.rollback()
            logger.exception("VOICE_IVR_CALLBACK_FAILED callSid=%s garage=%s", call_sid, garage.id)
    logger.warning(
        "VOICE_IVR_FALLBACK callSid=%s garage=%s reason=%s action=CALLBACK logged=%s",
        call_sid,
        garage.id,
        reason,
        logged,
    )
    response = VoiceResponse()
    _say(
        response,
        CLOSING_LINE
        if logged
        else "Sorry, we can't take your call right now. Please try again later. Goodbye.",
    )
    response.hangup()
    return response
