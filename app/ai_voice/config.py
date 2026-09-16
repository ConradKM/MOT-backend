"""OpenAI Realtime voice configuration gate - mirrors
app/communications/voice_relay.py::conversationrelay_enabled.
"""

from __future__ import annotations

from flask import current_app


def openai_voice_enabled() -> bool:
    """Whether this deployment should route inbound calls to the OpenAI
    Realtime bridge at all. Does not by itself mean a call can actually
    succeed - that also needs OPENAI_API_KEY set and a WebSocket-capable
    server; both are checked separately at the point of use so a
    misconfiguration degrades to the existing ConversationRelay/static
    behaviour rather than dropping the call."""
    return bool(current_app.config.get("OPENAI_VOICE_ENABLED"))


def openai_configured() -> bool:
    return bool(current_app.config.get("OPENAI_API_KEY"))


def realtime_ws_url() -> str:
    base = current_app.config.get("OPENAI_REALTIME_WS_URL", "wss://api.openai.com/v1/realtime")
    model = current_app.config.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
    return f"{base}?model={model}"
