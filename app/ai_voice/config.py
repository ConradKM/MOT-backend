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


def openai_voice_prerequisites() -> list[dict]:
    """Deployment-level OpenAI Realtime voice prerequisites, reported the
    same way ``embedded_signup_prerequisites`` reports WhatsApp's.

    Empty when ``OPENAI_VOICE_ENABLED`` is off - an unenabled feature is not
    a blocker, it's a feature nobody asked for yet.
    """
    if not openai_voice_enabled():
        return []
    cfg = current_app.config
    checks = [
        (
            "openai_api_key",
            "OpenAI API key",
            bool(cfg.get("OPENAI_API_KEY")),
            "Set OPENAI_API_KEY to a key for the project with Realtime access.",
        ),
        (
            "openai_webhook_secret",
            "OpenAI webhook secret",
            bool(cfg.get("OPENAI_WEBHOOK_SECRET")),
            (
                "Set OPENAI_WEBHOOK_SECRET to the signing secret for the "
                "realtime.call.incoming webhook, from the OpenAI dashboard."
            ),
        ),
    ]
    return [
        {"key": key, "label": label, "satisfied": ok, "how_to_fix": None if ok else fix}
        for key, label, ok, fix in checks
    ]
