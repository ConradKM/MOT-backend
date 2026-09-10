"""WhatsApp onboarding through Twilio's Tech Provider programme.

The shape of the flow, verified against Twilio's Tech Provider integration
guide and the Messaging **Senders v2** resource:

1. The business's own number is recorded first. Meta requires the full number
   to be known *before* Embedded Signup starts, because the sender is
   registered against it afterwards.
2. The business owner completes **Meta Embedded Signup** in a Meta-hosted
   window: they sign in, pick or create a Meta Business Portfolio, create a
   WhatsApp Business Account, and verify the number where Meta asks for it.
   CoMaz does not and must not automate this - it is Meta's consent step, and
   there is no API that replaces it.
3. Meta hands back a ``waba_id`` (and a ``phone_number_id``). CoMaz stores
   them against the business.
4. The sender is registered through Twilio with the **business's own
   subaccount credentials**, passing ``configuration.waba_id`` - which is what
   associates that WABA with that subaccount. No token exchange happens on
   our side; Twilio holds the Meta credential from the Partner Solution.
5. Twilio reports a sender status; CoMaz mirrors it into its own state
   machine and, on ONLINE, writes ``whatsapp_sender`` across to
   ``GarageCommunicationSettings`` so the existing inbound webhook resolves
   the tenant.

**Numbers already on WhatsApp.** If the number is in use by consumer WhatsApp
or the WhatsApp Business App, Meta will not let it be registered, and the fix
is to *delete that account* - a destructive act on the customer's own
property. CoMaz never does it: the state machine stops at
``EXISTING_WHATSAPP_MIGRATION_REQUIRED`` and tells the operator what to ask
for. See :func:`describe_existing_registration`.

**External prerequisites this module cannot create.** A live Meta app with
``whatsapp_business_messaging`` + ``whatsapp_business_management`` approved, an
Embedded Signup configuration id, and a Twilio Partner Solution id linked to
that Meta app. :func:`embedded_signup_config` reports exactly which of those
are missing rather than pretending the flow is ready.
"""

from __future__ import annotations

import logging
import secrets as _secrets

from flask import current_app
from twilio.base.exceptions import TwilioRestException

from app.models.garage import Garage

from .states import SENDER_STATUS_TO_STATE
from .subaccounts import SubaccountError, get_subaccount_client

logger = logging.getLogger(__name__)

WHATSAPP_INCOMING_PATH = "/api/webhooks/twilio/whatsapp/incoming"
WHATSAPP_STATUS_PATH = "/api/webhooks/twilio/whatsapp/status"

#: Meta Graph version the Embedded Signup JS SDK is initialised with. Pinned,
#: not floating: Meta changes the ES payload shape between versions.
DEFAULT_GRAPH_VERSION = "v21.0"


class WhatsAppProvisioningError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _twilio_error(exc: TwilioRestException, fallback: str) -> WhatsAppProvisioningError:
    return WhatsAppProvisioningError(
        exc.msg or fallback, code=str(exc.code) if exc.code is not None else None
    )


def sender_address(number_e164: str) -> str:
    """The Twilio "From" form of a WhatsApp number. Stored with the prefix
    because that is exactly what the Messages API needs - the same convention
    ``GarageCommunicationSettings.whatsapp_sender`` has always used."""
    return number_e164 if number_e164.startswith("whatsapp:") else f"whatsapp:{number_e164}"


def webhook_urls() -> dict[str, str]:
    base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    return {
        "callback_url": f"{base}{WHATSAPP_INCOMING_PATH}",
        "status_callback_url": f"{base}{WHATSAPP_STATUS_PATH}",
    }


# --------------------------------------------------------------------------
# Meta Embedded Signup
# --------------------------------------------------------------------------


def new_signup_state() -> str:
    """A nonce tying one Embedded Signup launch to one business.

    Required back on completion, so a completion payload captured from one
    admin's browser cannot be replayed against a different business.
    """
    return _secrets.token_urlsafe(32)


def embedded_signup_prerequisites() -> list[dict]:
    """Which external prerequisites are configured, and which are not.

    Deliberately reported rather than assumed: without these, Embedded Signup
    cannot run at all, and an operator needs to see *why* the button is
    disabled instead of watching a Meta window fail.
    """
    cfg = current_app.config
    checks = [
        (
            "meta_app_id",
            "Meta app ID",
            bool(cfg.get("META_APP_ID")),
            (
                "Set META_APP_ID to the Live Meta app approved for "
                "whatsapp_business_messaging and whatsapp_business_management."
            ),
        ),
        (
            "meta_config_id",
            "Embedded Signup configuration ID",
            bool(cfg.get("META_EMBEDDED_SIGNUP_CONFIG_ID")),
            (
                "Create an Embedded Signup configuration in the Meta app dashboard "
                "and set META_EMBEDDED_SIGNUP_CONFIG_ID."
            ),
        ),
        (
            "twilio_solution_id",
            "Twilio Partner Solution ID",
            bool(cfg.get("TWILIO_PARTNER_SOLUTION_ID")),
            (
                "Raise the Twilio Tech Provider ticket to have a Partner Solution "
                "created for your Meta app, then set TWILIO_PARTNER_SOLUTION_ID."
            ),
        ),
        (
            "public_https_origin",
            "Public HTTPS origin",
            (cfg.get("PUBLIC_API_BASE_URL") or "").startswith("https://"),
            (
                "Set PUBLIC_API_BASE_URL to this deployment's public HTTPS origin so "
                "Twilio can reach the WhatsApp webhooks."
            ),
        ),
    ]
    return [
        {"key": key, "label": label, "satisfied": ok, "how_to_fix": None if ok else fix}
        for key, label, ok, fix in checks
    ]


def embedded_signup_ready() -> bool:
    return all(item["satisfied"] for item in embedded_signup_prerequisites())


def embedded_signup_config(state: str) -> dict:
    """Everything the browser needs to launch Meta Embedded Signup.

    Public identifiers only - a Meta app id, an ES configuration id and a
    Twilio solution id are all values Meta itself renders into the popup URL.
    No Meta app secret, no access token and no Twilio credential is included,
    and none is needed: the flow returns a code to Meta's own window, and the
    WABA is associated server-side through Twilio afterwards.
    """
    cfg = current_app.config
    return {
        "ready": embedded_signup_ready(),
        "prerequisites": embedded_signup_prerequisites(),
        "app_id": cfg.get("META_APP_ID") or None,
        "config_id": cfg.get("META_EMBEDDED_SIGNUP_CONFIG_ID") or None,
        "solution_id": cfg.get("TWILIO_PARTNER_SOLUTION_ID") or None,
        "graph_version": cfg.get("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION,
        "state": state,
    }


def describe_existing_registration(number_e164: str) -> dict:
    """What to tell an operator whose customer's number is already on WhatsApp.

    CoMaz will not delete a WhatsApp account it does not own, so this returns
    instructions, not an action.
    """
    return {
        "phone_number": number_e164,
        "meaning": (
            "This number already has a WhatsApp or WhatsApp Business App account. "
            "Meta will not let the same number be registered on the WhatsApp Business "
            "Platform until that account is removed by whoever owns it."
        ),
        "steps": [
            (
                "Confirm with the business that they are ready to stop using the "
                "WhatsApp app on this number - their existing chat history stays on "
                "their device but will not move across."
            ),
            (
                "Ask them to delete the WhatsApp account for this number from within "
                "the WhatsApp or WhatsApp Business app (Settings → Account → Delete "
                "my account)."
            ),
            (
                "If the number is instead on another WhatsApp Business Platform "
                "provider, ask them to turn off two-factor authentication for it in "
                "WhatsApp Manager rather than deleting anything."
            ),
            (
                "Wait a few minutes after deletion for Meta to release the number, "
                "then mark the migration complete here and continue with Embedded "
                "Signup."
            ),
        ],
        "comaz_will_not": (
            "CoMaz never deletes, migrates or alters an existing WhatsApp registration on "
            "a customer's behalf."
        ),
    }


# --------------------------------------------------------------------------
# Twilio Senders v2
# --------------------------------------------------------------------------


def _senders(garage: Garage):
    """The Senders v2 resource, authenticated as the business's subaccount.

    Senders v2 has no parent-acting path, so this is one of the few places
    that genuinely needs the subaccount's own credentials.
    """
    client = get_subaccount_client(garage)
    return client.messaging.v2.channels_senders


class _RequestBody:
    """The request body for a Senders v2 call, as the API documents it.

    Twilio's SDK ships generated request classes for these calls, but their
    shape is not stable across *patch* releases: 9.11.0 stored whatever nested
    members you handed it, so callers had to construct
    ``MessagingV2ChannelsSenderConfiguration`` themselves, while 9.11.1
    constructs them from plain dicts and raises ``AttributeError`` if you pass
    a constructed one. ``requirements.txt`` pins only ``twilio>=9,<10``, so
    either shape can turn up in a deployment, and code written against one
    breaks on the other with no type error to warn you.

    Both releases' transport layers do exactly one thing with the request
    object: call ``.to_dict()`` and post the result as JSON. So this passes a
    body that is already the documented JSON, and depends on neither
    constructor. The nested dicts are the API's own field names, which is what
    the generated classes would have produced anyway.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict):
        # Drop keys whose value is None so an omitted section is absent from
        # the JSON rather than sent as null - Twilio treats the two
        # differently on update, where null can clear a stored value.
        self._payload = {key: value for key, value in payload.items() if value is not None}

    def to_dict(self) -> dict:
        return self._payload


def register_sender(
    garage: Garage,
    *,
    number_e164: str,
    waba_id: str,
    display_name: str,
    verification_method: str | None = None,
) -> dict:
    """Register this business's WhatsApp sender with Twilio.

    ``configuration.waba_id`` is what binds the customer's WABA to *this*
    subaccount - the association step of the Tech Provider flow. The webhook
    URLs are CoMaz's existing WhatsApp routes, so an inbound message resolves
    through ``tenant_resolution.py`` exactly like every other channel.
    """
    urls = webhook_urls()
    configuration: dict[str, object] = {"waba_id": waba_id}
    if verification_method:
        configuration["verification_method"] = verification_method

    payload = {
        "sender_id": sender_address(number_e164),
        "configuration": configuration,
        "webhook": {
            "callback_url": urls["callback_url"],
            "callback_method": "POST",
            "status_callback_url": urls["status_callback_url"],
            "status_callback_method": "POST",
        },
        "profile": {"name": display_name},
    }

    try:
        sender = _senders(garage).create(_RequestBody(payload))
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to register the WhatsApp sender.") from exc
    except SubaccountError as exc:
        raise WhatsAppProvisioningError(str(exc)) from exc

    logger.info(
        "[provisioning] registered WhatsApp sender %s (%s) for garage %s",
        sender.sid,
        sender.status,
        garage.id,
    )
    return _sender_snapshot(sender)


def submit_verification_code(garage: Garage, sender_sid: str, code: str) -> dict:
    """Hand Meta's one-time code to Twilio.

    The code is passed straight through and never persisted: it is a
    short-lived credential belonging to the customer's number, and there is no
    situation in which CoMaz needs to read it back.
    """
    try:
        sender = _senders(garage)(sender_sid).update({"configuration": {"verification_code": code}})
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio rejected the verification code.") from exc
    except SubaccountError as exc:
        raise WhatsAppProvisioningError(str(exc)) from exc
    return _sender_snapshot(sender)


def fetch_sender(garage: Garage, sender_sid: str) -> dict:
    """The sender's current state at Twilio - the Check status action."""
    try:
        sender = _senders(garage)(sender_sid).fetch()
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not return the sender's status.") from exc
    except SubaccountError as exc:
        raise WhatsAppProvisioningError(str(exc)) from exc
    return _sender_snapshot(sender)


def update_sender_webhooks(garage: Garage, sender_sid: str) -> dict:
    """Re-point a registered sender at CoMaz's WhatsApp webhooks.

    The repair action for a sender that was registered before this deployment
    moved origin, or configured by hand in the Twilio console.
    """
    urls = webhook_urls()
    try:
        sender = _senders(garage)(sender_sid).update(
            _RequestBody(
                {
                    "webhook": {
                        "callback_url": urls["callback_url"],
                        "callback_method": "POST",
                        "status_callback_url": urls["status_callback_url"],
                        "status_callback_method": "POST",
                    }
                }
            )
        )
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to update the sender's webhooks.") from exc
    except SubaccountError as exc:
        raise WhatsAppProvisioningError(str(exc)) from exc
    return _sender_snapshot(sender)


def _as_dict(value: object) -> dict:
    """Twilio returns these sub-objects as plain dicts over the wire, but the
    SDK types them loosely; normalise so callers never branch on it."""
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    return {}


def _sender_snapshot(sender: object) -> dict:
    """The subset of a Senders v2 resource CoMaz persists and displays."""
    webhook = _as_dict(getattr(sender, "webhook", None))
    profile = _as_dict(getattr(sender, "profile", None))
    configuration = _as_dict(getattr(sender, "configuration", None))
    offline_reasons = getattr(sender, "offline_reasons", None) or []

    return {
        "sid": getattr(sender, "sid", None),
        "status": getattr(sender, "status", None),
        "sender_id": getattr(sender, "sender_id", None),
        "waba_id": configuration.get("waba_id"),
        "display_name": profile.get("name"),
        "callback_url": webhook.get("callback_url"),
        "status_callback_url": webhook.get("status_callback_url"),
        "offline_reasons": [str(reason) for reason in offline_reasons],
    }


def state_for_sender_status(sender_status: str | None, *, current: str) -> str:
    """Our state for a Twilio sender status, leaving ``current`` alone when
    Twilio reports something we have no mapping for.

    Guessing would be worse than not moving: an unmapped status is far more
    likely to be a new Twilio value than a failure, and silently flipping a
    live business to FAILED is the expensive mistake here.
    """
    if not sender_status:
        return current
    return SENDER_STATUS_TO_STATE.get(sender_status, current)
