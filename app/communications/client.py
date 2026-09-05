"""Lazy, cached Twilio REST client.

Nothing here runs at import or app-startup time - the client is only built
the first time something actually tries to talk to Twilio, and only if it's
configured. Tests patch ``get_twilio_client`` directly (wherever it's
imported into a caller) rather than touching the real SDK.
"""

from flask import current_app
from twilio.rest import Client

from .config import is_twilio_configured

_EXT_KEY = "_twilio_client"


def get_twilio_client() -> Client | None:
    """The configured Twilio REST client, or ``None`` if Twilio isn't
    configured. Cached on the app's extensions dict, the same pattern as
    app/storage/__init__.py::get_storage."""
    if not is_twilio_configured():
        return None

    client = current_app.extensions.get(_EXT_KEY)
    if client is not None:
        return client

    client = Client(
        current_app.config["TWILIO_ACCOUNT_SID"],
        current_app.config["TWILIO_AUTH_TOKEN"],
    )
    current_app.extensions[_EXT_KEY] = client
    return client


def get_twilio_client_for_garage(garage) -> Client | None:
    """The Twilio client that should act on ``garage``'s behalf.

    Every garage runs from the platform master account today. Once a garage
    has its own ``twilio_subaccount_sid`` (see
    GarageCommunicationSettings), calls should be scoped to that subaccount
    instead - but a subaccount has its own Auth Token, which CoMaz OS does not
    yet have anywhere safe to store (see docs/TWILIO_SETUP.md). This function
    is the one place that will change when it does; nothing else in the
    codebase should reach for ``get_twilio_client()`` directly for a
    garage-specific send.
    """
    return get_twilio_client()
