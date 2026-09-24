"""Signed handoff from CoMaz's phone menu to the OpenAI Realtime SIP agent.

When a caller picks an AI option in the menu (app/communications/ivr), the
Twilio call is bridged with ``<Dial><Sip>`` to this deployment's OpenAI
project. Unlike the Elastic SIP trunk path there is no ``Diversion`` header
naming the dialled business, and a SIP header by itself is caller/network
controlled - anyone who knows the project's SIP URI could send one. So the
bridge carries custom ``X-CoMaz-*`` headers HMAC-signed with a key only this
backend holds, and app/ai_voice/routes.py trusts the tenant, Twilio CallSid
and route only when that signature verifies and is fresh.

A missing signature means "not a menu handoff" (the trunk path resolves the
tenant as before); a present-but-invalid one is rejected outright.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

from flask import current_app

from .tenant import sip_header

HEADER_GARAGE = "X-CoMaz-Garage"
HEADER_CALL = "X-CoMaz-Call"
HEADER_ROUTE = "X-CoMaz-Route"
HEADER_CALLER = "X-CoMaz-Caller"
HEADER_TS = "X-CoMaz-Ts"
HEADER_SIG = "X-CoMaz-Sig"

# Menu → SIP INVITE → OpenAI webhook takes seconds; five minutes absorbs
# clock skew and a slow ring without leaving a replay window worth having.
MAX_AGE_SECONDS = 300


class InvalidSipHandoff(ValueError):
    """A handoff header was present but its signature is wrong or stale."""


@dataclass(frozen=True)
class SipHandoff:
    garage_id: str
    twilio_call_sid: str
    route: str
    caller: str


def _key() -> bytes:
    secret = current_app.config.get("VOICE_SIP_SIGNING_SECRET") or current_app.config["SECRET_KEY"]
    # Derived rather than used raw, so this signature can never be confused
    # with anything else SECRET_KEY signs (JWTs).
    return hmac.new(secret.encode(), b"comaz-voice-sip-handoff-v1", hashlib.sha256).digest()


def _signature(garage_id: str, call_sid: str, route: str, caller: str, ts: str) -> str:
    message = f"v1|{garage_id}|{call_sid}|{route}|{caller}|{ts}".encode()
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()


def openai_sip_uri(
    *, garage_id: str, twilio_call_sid: str, route: str, caller: str, now: float | None = None
) -> str:
    """The ``<Sip>`` URI (with signed headers) that bridges this call to the
    OpenAI project. Raises ValueError if the project id is
    missing - callers check ai_voice_available() first."""
    project_id = current_app.config.get("OPENAI_PROJECT_ID")
    if not project_id:
        raise ValueError("OPENAI_PROJECT_ID is not configured.")
    ts = str(int(now if now is not None else time.time()))
    # Every value stays URI-safe so Twilio's header (un)escaping can never
    # change what was signed: the caller's E.164 travels without its '+'.
    caller = caller.lstrip("+")
    headers = {
        HEADER_GARAGE: garage_id,
        HEADER_CALL: twilio_call_sid,
        HEADER_ROUTE: route,
        HEADER_CALLER: caller,
        HEADER_TS: ts,
        HEADER_SIG: _signature(garage_id, twilio_call_sid, route, caller, ts),
    }
    query = "&".join(f"{name}={quote(value, safe='')}" for name, value in headers.items())
    return f"sip:{project_id}@sip.api.openai.com;transport=tls?{query}"


def verify(sip_headers: list, *, now: float | None = None) -> SipHandoff | None:
    """The verified handoff carried by an incoming OpenAI SIP call, None when
    the call carries no handoff at all, or InvalidSipHandoff."""
    signature = sip_header(sip_headers, HEADER_SIG)
    if not signature:
        return None

    def _value(name: str) -> str:
        return unquote(sip_header(sip_headers, name) or "").strip()

    garage_id = _value(HEADER_GARAGE)
    call_sid = _value(HEADER_CALL)
    route = _value(HEADER_ROUTE)
    caller = _value(HEADER_CALLER).lstrip("+")
    ts = _value(HEADER_TS)
    expected = _signature(garage_id, call_sid, route, caller, ts)
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise InvalidSipHandoff("bad-signature")
    try:
        age = (now if now is not None else time.time()) - int(ts)
    except ValueError as exc:
        raise InvalidSipHandoff("bad-timestamp") from exc
    if age > MAX_AGE_SECONDS or age < -MAX_AGE_SECONDS:
        raise InvalidSipHandoff("expired")
    if not garage_id or not call_sid or not route:
        raise InvalidSipHandoff("incomplete")
    return SipHandoff(
        garage_id=garage_id,
        twilio_call_sid=call_sid,
        route=route,
        caller=f"+{caller}" if caller else "",
    )
