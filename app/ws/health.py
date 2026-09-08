"""A trivial WebSocket endpoint that proves the transport is wired up end to
end - the browser/Twilio never uses it. ``GET /api/ws/ping`` upgraded to a
WebSocket echoes back whatever text it receives, and replies ``pong`` to an
empty frame. Used to verify the deployed gunicorn/gevent server can actually
serve a WebSocket before the ConversationRelay bridge is built on it.
"""

from __future__ import annotations

from app.extensions import sock


@sock.route("/api/ws/ping")
def ws_ping(ws) -> None:
    while True:
        message = ws.receive()
        if message is None:  # client closed
            break
        ws.send(message or "pong")
