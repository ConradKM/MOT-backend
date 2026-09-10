"""Lazy, cached Twilio REST client.

Nothing here runs at import or app-startup time - the client is only built
the first time something actually tries to talk to Twilio, and only if it's
configured. Tests patch ``get_twilio_client`` directly (wherever it's
imported into a caller) rather than touching the real SDK.

**Outbound REST authentication.** When ``TWILIO_API_KEY_SID`` /
``TWILIO_API_KEY_SECRET`` are both set the client authenticates with the API
key + Account SID (Twilio's recommendation for production - a key can be
rotated or revoked without touching the account). Otherwise it falls back to
Account SID + Auth Token, which is what dev/test and any deployment without a
key configured continue to use. Either way the webhook signature check
(``security.py``) still uses the Auth Token - that's the one place it is
genuinely required.
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

    cfg = current_app.config
    account_sid = cfg["TWILIO_ACCOUNT_SID"]
    api_key_sid = cfg.get("TWILIO_API_KEY_SID")
    api_key_secret = cfg.get("TWILIO_API_KEY_SECRET")

    if api_key_sid and api_key_secret:
        # API key auth: the SDK signature is Client(username, password,
        # account_sid); pass the key SID/secret as the credentials and the
        # Account SID explicitly so requests are still scoped to the account.
        client = Client(api_key_sid, api_key_secret, account_sid)
    else:
        client = Client(account_sid, cfg["TWILIO_AUTH_TOKEN"])

    current_app.extensions[_EXT_KEY] = client
    return client


def get_twilio_client_for_garage(garage) -> Client | None:
    """The Twilio client that should act on ``garage``'s behalf.

    A garage with its own ``twilio_subaccount_sid`` (allocated by Platform
    Admin - see app/communications/provisioning) is scoped to that
    subaccount, so its sends are billed, logged and rate-limited separately
    from every other tenant's. A garage without one still runs from the
    platform master account, which is the correct behaviour for every tenant
    onboarded before subaccounts existed.

    The scoping uses the **master** credentials against the subaccount's
    2010-04-01 resource path rather than the subaccount's own Auth Token: the
    classic REST API supports a parent acting on a subaccount's Messages and
    Calls, so the common send path never has to decrypt a stored credential.
    (Messaging Senders v2 has no such path and does need the subaccount's own
    token - that is why app/communications/provisioning/subaccounts.py exists
    and why only it reaches for one.)

    Returns ``None`` when Twilio isn't configured at all, exactly as before,
    so every caller's existing skip-and-log path is unchanged.
    """
    base = get_twilio_client()
    if base is None:
        return None

    settings = getattr(garage, "communication_settings", None)
    subaccount_sid = getattr(settings, "twilio_subaccount_sid", None) if settings else None
    if not subaccount_sid:
        return base

    cfg = current_app.config
    api_key_sid = cfg.get("TWILIO_API_KEY_SID")
    api_key_secret = cfg.get("TWILIO_API_KEY_SECRET")
    if api_key_sid and api_key_secret:
        return Client(api_key_sid, api_key_secret, subaccount_sid)
    return Client(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"], subaccount_sid)
