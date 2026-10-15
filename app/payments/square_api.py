"""Thin HTTP client for Square's REST API (payments, refunds, locations,
merchants, OAuth) - shared by the Square adapter
(app/payments/providers/square.py) and the OAuth connection module
(app/payments/square_oauth.py). Nothing outside those two files imports this.

Why the HTTP API directly rather than the official ``squareup`` SDK: CoMaz
needs about eight endpoints; the SDK is regenerated with a new major version
roughly monthly, each release fixes its own ``Square-Version``, and it pulls in
pydantic. A pinned ``Square-Version`` header (``SQUARE_API_VERSION``) over
plain ``httpx`` keeps response shapes stable until we deliberately bump it, and
lets tests mock the wire with ``httpx.MockTransport`` (see
tests/square_helpers.py) instead of an SDK's internal classes.

Never logs request bodies, response bodies, or headers - they can carry
tokens.
"""

from __future__ import annotations

from typing import Any

import httpx
from flask import current_app

_BASE_URLS = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}

#: Key in ``app.extensions`` a test can set to an ``httpx`` transport (e.g.
#: ``httpx.MockTransport``) so no request ever leaves the process.
TRANSPORT_EXTENSION_KEY = "square_http_transport"

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class SquareApiError(RuntimeError):
    """A Square API call failed. ``status`` is the HTTP status (0 for a
    network failure), ``code``/``category`` Square's first error entry, and
    ``body`` the parsed response (may hold a FAILED ``payment`` object for a
    card decline). The message is Square's human ``detail`` - never a token."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str | None = None,
        category: str | None = None,
        body: dict | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.category = category
        self.body = body or {}


def environment() -> str:
    env = str(current_app.config.get("SQUARE_ENVIRONMENT") or "sandbox").lower()
    return env if env in _BASE_URLS else "sandbox"


def base_url() -> str:
    return _BASE_URLS[environment()]


def _client() -> httpx.Client:
    transport = current_app.extensions.get(TRANSPORT_EXTENSION_KEY)
    return httpx.Client(base_url=base_url(), timeout=_TIMEOUT, transport=transport)


def request(
    method: str,
    path: str,
    *,
    bearer_token: str | None = None,
    client_secret: str | None = None,
    json: dict | None = None,
) -> dict[str, Any]:
    """One Square API call. ``bearer_token`` is a merchant access token;
    ``client_secret`` instead authenticates as the application (only the
    OAuth revoke endpoint wants that). Returns the parsed JSON body; raises
    :class:`SquareApiError` on any non-2xx or transport failure."""
    headers = {
        "Square-Version": str(current_app.config.get("SQUARE_API_VERSION")),
        "Accept": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif client_secret:
        headers["Authorization"] = f"Client {client_secret}"

    try:
        with _client() as client:
            response = client.request(method, path, headers=headers, json=json)
    except httpx.HTTPError as exc:
        # Type only: an httpx error's str() can include the request URL but
        # never headers; still, keep it terse.
        raise SquareApiError(
            f"Could not reach Square ({type(exc).__name__}).", status=0, code="network_error"
        ) from exc

    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if response.is_success:
        return body

    errors = body.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    # OAuth endpoints use a different error envelope ({"message", "type"}).
    message = (
        first.get("detail") or body.get("message") or f"Square API error {response.status_code}"
    )
    raise SquareApiError(
        str(message),
        status=response.status_code,
        code=first.get("code") or body.get("type"),
        category=first.get("category"),
        body=body,
    )
