"""A thin, synchronous client for OpenAI's Realtime API - the OpenAI side of
the bridge in app/ws/openai_voice.py.

Deliberately raw WebSocket protocol (via ``websocket-client``), not the
``openai`` Python SDK - the Realtime API's session-configuration shape is
still actively evolving (see docs/OPENAI_VOICE.md), and a hand-rolled client
here means every message this codebase sends/expects is visible in one place
rather than behind an SDK abstraction that could silently change behaviour on
an upgrade. Synchronous (not asyncio) so it cooperates correctly with
gunicorn's gevent worker, exactly like Flask-Sock itself (see app/ws/__init__.py).
"""

from __future__ import annotations

import json
import logging

import websocket

logger = logging.getLogger(__name__)

# Server events this bridge acts on. Every other server event type (session.
# created, response.audio_transcript.delta, rate_limits.updated, ...) is
# received and ignored - logged at debug level only, never a reason to fail
# the call.
EVENT_AUDIO_DELTA = "response.audio.delta"
EVENT_FUNCTION_CALL_DONE = "response.function_call_arguments.done"
EVENT_ERROR = "error"
EVENT_SESSION_CREATED = "session.created"


class RealtimeSession:
    """One OpenAI Realtime WebSocket connection, for the duration of one
    phone call. Not reused across calls - a fresh session per call keeps
    tenant context (instructions, tools) from ever leaking between callers.
    """

    def __init__(self, *, ws_url: str, api_key: str, connect_timeout: float = 10.0):
        self._ws = websocket.create_connection(
            ws_url,
            header=[f"Authorization: Bearer {api_key}"],
            timeout=connect_timeout,
        )

    def configure_session(
        self,
        *,
        instructions: str,
        tools: list[dict],
        voice: str,
        audio_format: str,
        model: str,
    ) -> None:
        """One ``session.update`` - instructions, tools, and the audio format
        matching Twilio Media Streams' own native codec (8kHz mu-law) so no
        transcoding happens anywhere in this codebase. ``audio_format`` is
        configurable (OPENAI_REALTIME_AUDIO_FORMAT) precisely because the
        exact accepted value for this is unverified as of writing - see
        docs/OPENAI_VOICE.md."""
        self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": model,
                    "output_modalities": ["audio"],
                    "instructions": instructions,
                    "audio": {
                        "input": {"format": {"type": audio_format}},
                        "output": {"format": {"type": audio_format}, "voice": voice},
                    },
                    "turn_detection": {"type": "semantic_vad"},
                    "tools": tools,
                },
            }
        )

    def send_caller_audio(self, base64_payload: str) -> None:
        """One chunk of the caller's audio, already base64-encoded exactly as
        Twilio sent it (see app/ws/openai_voice.py - no decode/re-encode)."""
        self._send({"type": "input_audio_buffer.append", "audio": base64_payload})

    def send_tool_result(self, call_id: str, output_json: str) -> None:
        self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_json,
                },
            }
        )
        self._send({"type": "response.create"})

    def receive(self) -> dict | None:
        """One server event, or ``None`` on a clean close. Blocks - call this
        from its own greenlet (see app/ws/openai_voice.py), never from the
        same one reading Twilio's socket."""
        try:
            raw = self._ws.recv()
        except websocket.WebSocketConnectionClosedException:
            return None
        if not raw:
            return None
        try:
            event: dict = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("[openai_voice] malformed server event, ignoring")
            return None
        return event

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            logger.debug("[openai_voice] error closing Realtime session", exc_info=True)

    def _send(self, payload: dict) -> None:
        self._ws.send(json.dumps(payload))
