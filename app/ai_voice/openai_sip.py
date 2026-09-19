"""OpenAI Realtime Calls API - accept/reject/hangup/refer a SIP call, and
verify the `realtime.call.incoming` webhook.

Uses the official ``openai`` SDK exclusively (``client.realtime.calls.*``,
``client.webhooks.unwrap``) rather than hand-rolled HTTP/signature code - see
https://developers.openai.com/api/docs/guides/voice-sip and
https://developers.openai.com/api/docs/guides/webhooks.

No audio passes through this module, or through this backend at all: a
Twilio Elastic SIP Trunk dials OpenAI's SIP endpoint directly
(sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls - see
docs/OPENAI_VOICE_SETUP.md), so the only things this backend ever sends
OpenAI are REST calls (this module) and Realtime *events* over the
call-control WebSocket (app/ai_voice/call_controller.py) - never a media
frame.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, cast

from flask import current_app
from openai import InvalidWebhookSignatureError, OpenAI, OpenAIError
from openai.types.realtime.realtime_audio_config_param import RealtimeAudioConfigParam
from openai.types.realtime.realtime_tools_config_param import RealtimeToolsConfigParam
from openai.types.webhooks.unwrap_webhook_event import UnwrapWebhookEvent


class OpenAIVoiceError(RuntimeError):
    """A Realtime Calls API call failed (including a real SDK/network error
    - see :func:`_reraise_as_voice_error`), or was attempted before it could
    succeed (no API key configured). app/ai_voice/routes.py and
    app/ai_voice/call_controller.py catch only this one exception type, never
    the openai SDK's own hierarchy directly, so a network blip degrades
    cleanly instead of propagating as a 500 out of a webhook handler."""


def _client() -> OpenAI:
    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIVoiceError("OpenAI is not configured for this deployment.")
    return OpenAI(api_key=api_key)


@contextmanager
def _reraise_as_voice_error(action: str):
    try:
        yield
    except OpenAIError as exc:
        raise OpenAIVoiceError(f"OpenAI {action} failed: {exc}") from exc


def verify_webhook(payload: bytes, headers) -> UnwrapWebhookEvent:
    """Verify + parse one webhook delivery. Raises
    ``openai.InvalidWebhookSignatureError`` on a bad/missing signature - the
    caller must reject with 400 and process nothing (see
    app/ai_voice/routes.py). ``headers`` must be the raw request headers
    (case-insensitive mapping), and ``payload`` the exact raw request body -
    verification is against the literal bytes that were signed."""
    secret = current_app.config.get("OPENAI_WEBHOOK_SECRET")
    if not secret:
        raise OpenAIVoiceError("OpenAI webhook verification is not configured.")
    # A secret pasted into a dashboard env var routinely picks up a trailing
    # newline/space from the clipboard - invisible in the UI, but it changes
    # the HMAC key entirely, so every delivery fails signature verification
    # with no clue why. Strip it defensively; a real secret never contains
    # leading/trailing whitespace.
    secret = secret.strip()
    # Verification itself is local (no network call) - client construction
    # just needs *a* key, not necessarily a valid one, but this deployment
    # needs a real OPENAI_API_KEY for every other call in this module anyway.
    client = _client()
    try:
        return client.webhooks.unwrap(payload, headers, secret=secret)
    except ValueError as exc:
        # The SDK raises a bare ValueError (not its own
        # InvalidWebhookSignatureError) when a required header is missing
        # entirely, e.g. a delivery that isn't really from OpenAI at all -
        # still an authentication failure, not a 500.
        raise InvalidWebhookSignatureError(str(exc)) from exc


# Re-exported so callers only need to import this module, not ``openai``
# itself, to catch a bad signature.
__all__ = [
    "InvalidWebhookSignatureError",
    "OpenAIVoiceError",
    "accept_call",
    "hangup_call",
    "refer_call",
    "reject_call",
    "verify_webhook",
]


def accept_call(call_id: str, *, instructions: str, tools: list[dict[str, Any]]) -> None:
    """Accept a pending `realtime.call.incoming` call, configuring the whole
    session (model/voice/audio format/instructions/tools) in this one call -
    per docs, this is the only place session config happens for a SIP call;
    nothing further needs to be sent over the control WebSocket."""
    cfg = current_app.config
    client = _client()
    audio_format = {"type": cfg.get("OPENAI_REALTIME_AUDIO_FORMAT", "audio/pcmu")}
    # Built as plain dicts (this deployment's own config values, not
    # attacker input) and cast to the SDK's TypedDicts - the openai SDK
    # doesn't export runtime constructors for these, only type hints.
    audio = cast(
        RealtimeAudioConfigParam,
        {
            "input": {"format": audio_format, "turn_detection": {"type": "semantic_vad"}},
            "output": {
                "format": audio_format,
                "voice": cfg.get("OPENAI_REALTIME_VOICE", "marin"),
            },
        },
    )
    with _reraise_as_voice_error("accept"):
        client.realtime.calls.accept(
            call_id,
            type="realtime",
            model=cfg.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
            instructions=instructions,
            audio=audio,
            tools=cast(RealtimeToolsConfigParam, tools),
        )


def reject_call(call_id: str, *, status_code: int | None = None) -> None:
    """Decline a pending call - the tenant-safety gate for an unknown/
    unprovisioned number. ``status_code`` is a SIP response code (e.g. 486
    Busy Here); omitted defaults to OpenAI's own 603 Decline."""
    client = _client()
    with _reraise_as_voice_error("reject"):
        if status_code is not None:
            client.realtime.calls.reject(call_id, status_code=status_code)
        else:
            client.realtime.calls.reject(call_id)


def hangup_call(call_id: str) -> None:
    """End an in-progress call from the backend side - used after a
    request_human_handoff tool call has let the model say its closing line
    (see app/ai_voice/call_controller.py)."""
    client = _client()
    with _reraise_as_voice_error("hangup"):
        client.realtime.calls.hangup(call_id)


def refer_call(call_id: str, target_uri: str) -> None:
    """Blind-transfer a live call via SIP REFER (e.g. ``tel:+441234567890``
    or a ``sip:`` URI) - see docs/OPENAI_VOICE_SETUP.md's handoff section.
    OpenAI documents this as blind transfer only; there is no attended/warm
    transfer primitive."""
    client = _client()
    with _reraise_as_voice_error("refer"):
        client.realtime.calls.refer(call_id, target_uri=target_uri)
