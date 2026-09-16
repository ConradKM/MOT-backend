"""``<Connect><Stream>`` TwiML for an inbound call routed to the OpenAI
Realtime bridge, and the ``wss://`` URL of the bridge it points at.

Mirrors app/communications/voice_relay.py's shape exactly, but uses Twilio
Media Streams (raw audio, bidirectional under ``<Connect>``) rather than
ConversationRelay (Twilio-side STT/TTS, text-only) - the bridge at
app/ws/openai_voice.py needs the caller's raw audio to forward to OpenAI's
Realtime API, which does its own speech understanding and synthesis.
"""

from __future__ import annotations

from flask import current_app
from twilio.twiml.voice_response import VoiceResponse

WS_PATH = "/api/ws/twilio/openai-voice"


def bridge_ws_url() -> str:
    """The wss:// URL Twilio is told to connect to - derived from
    PUBLIC_API_BASE_URL, exactly like voice_relay.py::bridge_ws_url."""
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}{WS_PATH}"


def build_openai_voice_twiml(garage) -> str:
    """TwiML for a resolved inbound call. Raises on a genuine build error so
    the caller (voice_webhooks.py) can fall back to the existing
    ConversationRelay/static behaviour."""
    response = VoiceResponse()
    connect = response.connect()
    stream = connect.stream(url=bridge_ws_url())
    # Passed back to the bridge in Media Streams' "start" message's
    # customParameters - cross-checked there against the number Twilio says
    # was dialled, exactly like app/ws/twilio_voice.py already does for
    # ConversationRelay, so a spoofed connection can never claim another
    # tenant.
    stream.parameter(name="garage_id", value=str(garage.id))
    return str(response)
