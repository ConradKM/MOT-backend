"""OpenAI Realtime (direct SIP) voice configuration gate."""

from __future__ import annotations

from flask import current_app


def openai_voice_enabled() -> bool:
    """Whether this deployment answers calls with the OpenAI Realtime SIP
    assistant at all. A business still separately needs a Twilio Elastic SIP
    Trunk pointed at OpenAI (see docs/OPENAI_VOICE_SETUP.md) - this flag only
    gates whether CoMaz's webhook/tool-calling side is active."""
    return bool(current_app.config.get("OPENAI_VOICE_ENABLED"))


def openai_configured() -> bool:
    return bool(current_app.config.get("OPENAI_API_KEY"))


def openai_webhook_configured() -> bool:
    return bool(current_app.config.get("OPENAI_WEBHOOK_SECRET"))
