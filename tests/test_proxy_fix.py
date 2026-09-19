"""Render terminates TLS and proxies every request through exactly one
internal hop before it reaches this container, so without ProxyFix,
``request.remote_addr`` (and anything keyed on it - see
PUBLIC_BOOKING_RATELIMIT/PUBLIC_AVAILABILITY_RATELIMIT in app/config.py)
sees Render's own proxy IP for every request, never the real client's -
silently turning a per-visitor rate limit into one shared by every customer
of every business on the platform. See app/__init__.py::create_app.

Asserts against a real request through the app's actual WSGI callable
(``client.get(...)``) rather than ``test_request_context``, which bypasses
the wrapped ``app.wsgi_app`` entirely and so can't observe ProxyFix at all.
"""

from flask import request

from app import create_app
from app.config import TestConfig


def _make_app_with_remote_addr_probe():
    app = create_app(TestConfig)

    @app.route("/__remote_addr_probe__")
    def _probe():
        return {"remote_addr": request.remote_addr}

    return app


def test_remote_addr_reflects_the_real_client_behind_renders_proxy():
    app = _make_app_with_remote_addr_probe()
    client = app.test_client()

    # Simulates exactly what Render's own edge proxy does: connects to this
    # container from its own internal IP, forwarding the real client's IP in
    # X-Forwarded-For - the one header a single trusted hop can't have forged
    # by the actual client, since the proxy always appends its own view of
    # the connecting address.
    response = client.get(
        "/__remote_addr_probe__",
        headers={"X-Forwarded-For": "203.0.113.7"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.1"},  # Render's own proxy IP
    )

    assert response.status_code == 200
    assert response.get_json()["remote_addr"] == "203.0.113.7"


def test_remote_addr_falls_back_to_the_direct_connection_with_no_forwarded_header():
    """Local dev / a direct connection with no proxy in front must keep
    working exactly as before - ProxyFix only acts on a header that's
    actually present."""
    app = _make_app_with_remote_addr_probe()
    client = app.test_client()

    response = client.get("/__remote_addr_probe__", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 200
    assert response.get_json()["remote_addr"] == "127.0.0.1"
