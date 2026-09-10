"""Twilio subaccounts: one per CoMaz business, created and resolved here.

Why a subaccount at all: Twilio's Tech Provider programme requires that each
customer WhatsApp Business Account map to a single Twilio account or
subaccount, and the same boundary is what keeps one business's numbers,
senders, logs and spend separate from another's. CoMaz therefore treats
"business without a subaccount" as an un-provisioned business, not as a
business running on the master account.

Two clients, deliberately distinct:

* the **master** client (``app/communications/client.py::get_twilio_client``)
  creates subaccounts and buys numbers into them, using this deployment's own
  credentials;
* a **subaccount** client, built here, acts *as* the business - which the
  Messaging Senders v2 API requires, since it has no
  ``/Accounts/{sid}/…`` path a parent can act through.

The subaccount's Auth Token is stored encrypted (``app/communications/
secrets.py``) and is read only inside this module. It is never returned to a
caller, never serialised, and never logged.
"""

from __future__ import annotations

import logging

from flask import current_app
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.communications.client import get_twilio_client
from app.communications.config import is_twilio_configured
from app.communications.secrets import (
    CURRENT_KEY_VERSION,
    SecretDecryptionError,
    SecretsNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
    secrets_configured,
)
from app.extensions import db
from app.models.communications.comms_onboarding import TwilioSubaccountCredential
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.models.garage import Garage

logger = logging.getLogger(__name__)


class SubaccountError(RuntimeError):
    """A subaccount could not be created or used. Carries a provider code when
    Twilio supplied one, so the caller can persist it as ``last_error_code``."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def subaccount_friendly_name(garage: Garage) -> str:
    """What this business's subaccount is called in the Twilio console.

    The slug is included because it is immutable and unique, so an operator
    looking at Twilio can always map a subaccount back to exactly one CoMaz
    business even after the business renames itself.
    """
    return f"CoMaz — {garage.name} ({garage.slug})"[:64]


def _settings_for(garage: Garage) -> GarageCommunicationSettings:
    settings = garage.communication_settings
    if settings is None:
        settings = GarageCommunicationSettings(garage_id=garage.id)
        db.session.add(settings)
        db.session.flush()
    return settings


def _claimed_by_other(subaccount_sid: str, garage: Garage) -> Garage | None:
    """The *other* business already using this subaccount SID, if any."""
    row = (
        db.session.query(GarageCommunicationSettings)
        .filter(
            GarageCommunicationSettings.twilio_subaccount_sid == subaccount_sid,
            GarageCommunicationSettings.garage_id != garage.id,
        )
        .first()
    )
    return row.garage if row else None


def store_credential(subaccount_sid: str, auth_token: str) -> None:
    """Encrypt and persist one subaccount's Auth Token, replacing any previous
    ciphertext for the same SID."""
    row = (
        db.session.query(TwilioSubaccountCredential)
        .filter_by(subaccount_sid=subaccount_sid)
        .first()
    )
    ciphertext = encrypt_secret(auth_token)
    if row is None:
        row = TwilioSubaccountCredential(
            subaccount_sid=subaccount_sid,
            auth_token_encrypted=ciphertext,
            key_version=CURRENT_KEY_VERSION,
        )
        db.session.add(row)
    else:
        row.auth_token_encrypted = ciphertext
        row.key_version = CURRENT_KEY_VERSION


def has_credential(subaccount_sid: str | None) -> bool:
    """Whether a usable ciphertext exists for this subaccount. Does not
    decrypt - a cheap check for the console's "can we act as this business?"
    indicator."""
    if not subaccount_sid:
        return False
    return (
        db.session.query(TwilioSubaccountCredential.id)
        .filter_by(subaccount_sid=subaccount_sid)
        .first()
        is not None
    )


def create_subaccount(garage: Garage) -> str:
    """Create (or adopt) this business's dedicated Twilio subaccount.

    Idempotent by design: a business that already has a
    ``twilio_subaccount_sid`` keeps it, and this returns that SID rather than
    creating a second one - a double-click in the console must not leave a
    stray subaccount behind.
    """
    settings = _settings_for(garage)
    if settings.twilio_subaccount_sid:
        return settings.twilio_subaccount_sid

    if not is_twilio_configured():
        raise SubaccountError(
            "Twilio is not configured for this deployment - set TWILIO_ACCOUNT_SID "
            "and TWILIO_AUTH_TOKEN before provisioning subaccounts."
        )
    if not secrets_configured():
        raise SubaccountError(
            "COMMS_SECRET_KEY is not set, so this subaccount's Auth Token could not "
            "be stored safely. Set it before creating subaccounts."
        )

    client = get_twilio_client()
    if client is None:  # pragma: no cover - guarded by is_twilio_configured above
        raise SubaccountError("Twilio client unavailable.")

    try:
        account = client.api.v2010.accounts.create(friendly_name=subaccount_friendly_name(garage))
    except TwilioRestException as exc:
        raise SubaccountError(
            exc.msg or "Twilio refused to create the subaccount.",
            code=str(exc.code) if exc.code is not None else None,
        ) from exc

    other = _claimed_by_other(account.sid, garage)
    if other is not None:  # pragma: no cover - a fresh SID cannot already exist
        raise SubaccountError(f"Twilio returned a subaccount already assigned to {other.name}.")

    try:
        store_credential(account.sid, account.auth_token)
    except SecretsNotConfiguredError as exc:
        raise SubaccountError(str(exc)) from exc

    settings.twilio_subaccount_sid = account.sid
    db.session.flush()
    # SID only. The Auth Token that came back on the same object must never
    # reach a log line.
    logger.info("[provisioning] created Twilio subaccount %s for garage %s", account.sid, garage.id)
    return str(account.sid)


def adopt_subaccount(garage: Garage, subaccount_sid: str, auth_token: str) -> str:
    """Attach an **existing** Twilio subaccount to this business.

    For a subaccount created by hand in the Twilio console before this
    console existed. Refused if another CoMaz business already claims that
    SID: two tenants on one subaccount is exactly the cross-tenant mix-up the
    unique constraint on ``twilio_subaccount_sid`` exists to prevent, and a
    clear error here is better than a database integrity error later.
    """
    if not secrets_configured():
        raise SubaccountError(
            "COMMS_SECRET_KEY is not set, so this subaccount's Auth Token could not "
            "be stored safely."
        )

    other = _claimed_by_other(subaccount_sid, garage)
    if other is not None:
        raise SubaccountError(
            f"Twilio subaccount {subaccount_sid} is already assigned to {other.name}. "
            "Each business must have its own subaccount."
        )

    settings = _settings_for(garage)
    if settings.twilio_subaccount_sid and settings.twilio_subaccount_sid != subaccount_sid:
        raise SubaccountError(
            f"{garage.name} is already on subaccount {settings.twilio_subaccount_sid}. "
            "Moving a live business between subaccounts is not supported here."
        )

    try:
        store_credential(subaccount_sid, auth_token)
    except SecretsNotConfiguredError as exc:
        raise SubaccountError(str(exc)) from exc

    settings.twilio_subaccount_sid = subaccount_sid
    db.session.flush()
    return subaccount_sid


def get_subaccount_client(garage: Garage) -> Client:
    """A Twilio REST client authenticated **as** this business's subaccount.

    Required by any API with no parent-acting path - Messaging Senders v2
    above all. Raises rather than silently falling back to the master account:
    registering a customer's WhatsApp sender on the platform account would
    put two tenants' senders in one place, which is the failure this whole
    module exists to prevent.
    """
    settings = garage.communication_settings
    subaccount_sid = settings.twilio_subaccount_sid if settings else None
    if not subaccount_sid:
        raise SubaccountError(
            f"{garage.name} has no Twilio subaccount yet - create one before registering senders."
        )

    row = (
        db.session.query(TwilioSubaccountCredential)
        .filter_by(subaccount_sid=subaccount_sid)
        .first()
    )
    if row is None:
        raise SubaccountError(
            f"No stored credential for subaccount {subaccount_sid}. Re-attach the "
            "subaccount with its Auth Token before continuing."
        )

    try:
        token = decrypt_secret(row.auth_token_encrypted)
    except (SecretDecryptionError, SecretsNotConfiguredError) as exc:
        raise SubaccountError(str(exc)) from exc

    return Client(subaccount_sid, token)


def get_client_for_subaccount_resources(garage: Garage) -> Client:
    """The client to use for **2010-04-01** resources owned by a subaccount -
    phone numbers, calls, applications.

    Here the master credentials are correct and preferred: Twilio's classic
    REST API lets a parent account act on a subaccount's resources through
    ``/2010-04-01/Accounts/{subaccount_sid}/…``, which the SDK expresses as a
    third constructor argument. Using the parent means no decryption happens
    at all for the common voice paths, and a lost subaccount token never
    blocks number management.
    """
    if not is_twilio_configured():
        raise SubaccountError("Twilio is not configured for this deployment.")

    settings = garage.communication_settings
    subaccount_sid = settings.twilio_subaccount_sid if settings else None
    if not subaccount_sid:
        raise SubaccountError(
            f"{garage.name} has no Twilio subaccount yet - create one before buying a number."
        )

    cfg = current_app.config
    api_key_sid = cfg.get("TWILIO_API_KEY_SID")
    api_key_secret = cfg.get("TWILIO_API_KEY_SECRET")
    if api_key_sid and api_key_secret:
        return Client(api_key_sid, api_key_secret, subaccount_sid)
    return Client(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"], subaccount_sid)
