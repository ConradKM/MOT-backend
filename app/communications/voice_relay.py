"""``<Connect><ConversationRelay>`` TwiML for an inbound call, and the
``wss://`` URL of the bridge it points at.

The bridge itself is ``app/ws/twilio_voice.py`` - this module only builds the
TwiML the existing ``voice_webhooks.py`` returns when
``TWILIO_CONVERSATIONRELAY_ENABLED`` is on and a business resolved. Everything
about *what the assistant says* still lives in ``app/conversation`` - this is
pure transport wiring.
"""

from __future__ import annotations

from flask import current_app
from twilio.twiml.voice_response import VoiceResponse

from app.conversation import actions

# The flask-sock route the ConversationRelay WebSocket connects to.
WS_PATH = "/api/ws/twilio/voice"

# Twilio caps the hints attribute; keep well under it.
_HINTS_MAX_CHARS = 800


def conversationrelay_enabled() -> bool:
    return bool(current_app.config.get("TWILIO_CONVERSATIONRELAY_ENABLED"))


def bridge_ws_url() -> str:
    """The wss:// URL Twilio is told to connect to - derived from
    ``PUBLIC_API_BASE_URL`` (the same value webhook signatures are checked
    against, so the two agree by construction)."""
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}{WS_PATH}"


def _speech_hints(garage) -> str:
    """Transcription hints from this business's own configuration - the name
    and its active service names. Not automotive-specific."""
    candidates = [garage.name, *[t.name for t in actions.get_appointment_types(garage)]]
    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        value = (raw or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return ", ".join(out)[:_HINTS_MAX_CHARS]


def build_incoming_call_twiml(garage) -> str:
    """The ConversationRelay TwiML for a resolved inbound call. Raises on a
    genuine build error so the caller can fall back to the static greeting."""
    cfg = current_app.config
    greeting = (
        f"Thanks for calling {garage.name}. I'm the automated booking assistant. How can I help?"
    )

    response = VoiceResponse()
    connect = response.connect()
    relay = connect.conversation_relay(
        url=bridge_ws_url(),
        welcome_greeting=greeting,
        welcome_greeting_interruptible="speech",
        language=cfg.get("CONVERSATIONRELAY_LANGUAGE") or "en-GB",
        tts_provider=cfg.get("CONVERSATIONRELAY_TTS_PROVIDER") or None,
        voice=cfg.get("CONVERSATIONRELAY_VOICE") or None,
        interruptible="speech",
        dtmf_detection=True,
        hints=_speech_hints(garage) or None,
    )
    # Passed back to the bridge in the WebSocket `setup` message's
    # customParameters - cross-checked there against the number Twilio says was
    # dialled, so a spoofed connection can't claim another tenant.
    relay.parameter(name="garage_id", value=str(garage.id))
    relay.parameter(name="business_name", value=garage.name)
    return str(response)
