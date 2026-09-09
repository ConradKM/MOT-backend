"""Browser (Twilio Voice SDK) outbound calling.

Staff place calls from the Communications UI. The browser holds a
short-lived **Voice Access Token** (minted by
``GET /api/communications/voice/token``) and connects a WebRTC call; Twilio
then fetches instructions from ``POST /api/communications/voice/outbound``
(the TwiML Application's Voice URL), which returns a ``<Dial>`` from the
business's own configured number.

This is a completely separate path from the inbound ConversationRelay
assistant (``app/ws/twilio_voice.py``) - it shares only the Twilio account
credentials and the call-log table.

Security model:

* the token is minted per employee, per garage, by an authenticated
  endpoint, so a staff member can only ever call on behalf of their own
  business;
* the caller ID on the outbound leg is **always** the garage's configured
  ``voice_phone_number``, set server-side here - never anything the browser
  sends, so a client cannot spoof another number;
* ``/outbound`` re-derives the garage from the signed client identity and
  refuses a number that isn't a valid UK number.
"""

from __future__ import annotations

import uuid

from flask import current_app
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

# client identity = "<PREFIX>-<garage id hex>-<employee id hex>" (no ":" so it
# survives Twilio's "client:<identity>" From address; no "-" inside the hex).
_IDENTITY_PREFIX = "cbz"


def browser_calling_configured() -> bool:
    """Whether outbound browser calling can run: an API key (Access Tokens
    must be signed with one, never the Auth Token), a TwiML App, and an
    Account SID."""
    cfg = current_app.config
    return bool(
        cfg.get("TWILIO_ACCOUNT_SID")
        and cfg.get("TWILIO_API_KEY_SID")
        and cfg.get("TWILIO_API_KEY_SECRET")
        and cfg.get("TWILIO_TWIML_APP_SID")
    )


def client_identity(garage_id: uuid.UUID, employee_id: uuid.UUID) -> str:
    return f"{_IDENTITY_PREFIX}-{uuid.UUID(str(garage_id)).hex}-{uuid.UUID(str(employee_id)).hex}"


def parse_client_identity(raw: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """``(garage_id, employee_id)`` from a token identity or a
    ``client:<identity>`` From address, or ``None`` if it isn't one of ours."""
    if not raw:
        return None
    value = raw.removeprefix("client:")
    parts = value.split("-")
    if len(parts) != 3 or parts[0] != _IDENTITY_PREFIX:
        return None
    try:
        return uuid.UUID(hex=parts[1]), uuid.UUID(hex=parts[2])
    except ValueError:
        return None


def build_voice_access_token(garage, employee) -> tuple[str, str]:
    """A signed, short-lived Voice Access Token for this employee to place
    browser calls for this garage. Returns ``(jwt, identity)``.

    Only ``outgoing`` is granted - the browser can place calls but is not
    registered to receive any."""
    cfg = current_app.config
    identity = client_identity(garage.id, employee.id)
    token = AccessToken(
        cfg["TWILIO_ACCOUNT_SID"],
        cfg["TWILIO_API_KEY_SID"],
        cfg["TWILIO_API_KEY_SECRET"],
        identity=identity,
        ttl=int(cfg.get("TWILIO_VOICE_TOKEN_TTL", 3600)),
    )
    token.add_grant(
        VoiceGrant(
            outgoing_application_sid=cfg["TWILIO_TWIML_APP_SID"],
            incoming_allow=False,
        )
    )
    return token.to_jwt(), identity
