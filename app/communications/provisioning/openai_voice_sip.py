"""Per-tenant Elastic SIP Trunk provisioning for OpenAI Voice.

Why a trunk per subaccount, not one shared trunk: Twilio's newer v1 API
domains - Trunking among them - have no parent-acts-as-subaccount path the
way the classic 2010-04-01 REST API does (see ``subaccounts.py``'s own
``get_client_for_subaccount_resources`` docstring for that older mechanism,
and why it does not extend here). A Trunk's ``PhoneNumbers`` subresource can
only reference numbers already owned by the *same* account that owns the
Trunk, so a shared, platform-owned trunk could never carry a number living
in a tenant's own isolated subaccount. Each business therefore gets its own
trunk, created with that business's own subaccount credentials
(``subaccounts.get_subaccount_client``), pointed at the same shared OpenAI
project/SIP destination every trunk uses.

The existing platform reference trunk (``TKf170893068fae837c390ccbd06b06fb9``,
"OpenAI Realtime Voice") lives on the parent account and is never touched,
read, or referenced by any function here - it predates this per-tenant
model and stays exactly as it is.
"""

from __future__ import annotations

import logging

from flask import current_app
from twilio.base.exceptions import TwilioRestException

from app.communications.config import is_twilio_configured
from app.models.garage import Garage

from .subaccounts import get_subaccount_client

logger = logging.getLogger(__name__)

ORIGINATION_URL_FRIENDLY_NAME = "OpenAI Realtime"


class OpenAIVoiceProvisioningError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _twilio_error(exc: TwilioRestException, fallback: str) -> OpenAIVoiceProvisioningError:
    return OpenAIVoiceProvisioningError(
        exc.msg or fallback, code=str(exc.code) if exc.code is not None else None
    )


def trunk_friendly_name(garage: Garage) -> str:
    return f"CoMaz — {garage.name} (OpenAI Voice)"[:64]


def openai_sip_origination_uri() -> str:
    project_id = current_app.config.get("OPENAI_PROJECT_ID")
    if not project_id:
        raise OpenAIVoiceProvisioningError(
            "OPENAI_PROJECT_ID is not configured for this deployment.", code="not_configured"
        )
    return f"sip:{project_id}@sip.api.openai.com;transport=tls"


def get_or_create_tenant_trunk(garage: Garage) -> str:
    """This business's own Elastic SIP Trunk SID, creating one if none
    exists yet.

    Idempotent against a crash between a successful Twilio create and the
    caller recording the SID: trunks are listed by friendly name first, and
    an existing match is reused rather than creating a second trunk for the
    same business.
    """
    if not is_twilio_configured():
        raise OpenAIVoiceProvisioningError("Twilio is not configured for this deployment.")

    client = get_subaccount_client(garage)
    name = trunk_friendly_name(garage)

    try:
        existing = client.trunking.v1.trunks.list(limit=20)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list this business's SIP trunks.") from exc

    for trunk in existing:
        if trunk.friendly_name == name:
            logger.info(
                "[provisioning] reusing existing SIP trunk %s for garage %s instead of "
                "creating another",
                trunk.sid,
                garage.id,
            )
            return str(trunk.sid)

    try:
        trunk = client.trunking.v1.trunks.create(friendly_name=name, secure=True)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio refused to create a SIP trunk for this business.") from exc

    logger.info("[provisioning] created SIP trunk %s for garage %s", trunk.sid, garage.id)
    return str(trunk.sid)


def ensure_origination_url(garage: Garage, trunk_sid: str) -> None:
    """Point this trunk's Origination at the shared OpenAI SIP destination -
    safe to call repeatedly, since an existing matching URL is left alone
    rather than duplicated."""
    client = get_subaccount_client(garage)
    sip_url = openai_sip_origination_uri()

    try:
        existing = client.trunking.v1.trunks(trunk_sid).origination_urls.list(limit=20)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list this trunk's origination URLs.") from exc

    if any(url.sip_url == sip_url for url in existing):
        return

    try:
        client.trunking.v1.trunks(trunk_sid).origination_urls.create(
            weight=1,
            priority=1,
            enabled=True,
            friendly_name=ORIGINATION_URL_FRIENDLY_NAME,
            sip_url=sip_url,
        )
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio refused to point this business's trunk at OpenAI."
        ) from exc


def ensure_number_associated(garage: Garage, trunk_sid: str, number_sid: str) -> None:
    """Associate this business's voice number with its own trunk - safe to
    call repeatedly, since an already-associated number is left alone
    rather than re-added (Twilio rejects a duplicate association anyway,
    but checking first keeps this action's own retries silent)."""
    client = get_subaccount_client(garage)

    try:
        existing = client.trunking.v1.trunks(trunk_sid).phone_numbers.list(limit=20)
    except TwilioRestException as exc:
        raise _twilio_error(exc, "Twilio could not list this trunk's numbers.") from exc

    if any(number.sid == number_sid for number in existing):
        return

    try:
        client.trunking.v1.trunks(trunk_sid).phone_numbers.create(phone_number_sid=number_sid)
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio refused to associate this number with the business's trunk."
        ) from exc


def enable_openai_voice(garage: Garage, number_sid: str) -> str:
    """Full provisioning sequence for one business: reuse-or-create its own
    trunk, point it at OpenAI, associate its voice number. Returns the
    trunk SID.

    Every step is independently idempotent (see each function's own
    docstring), so calling this again after a partial failure - a crash
    between trunk creation and the origination URL step, for example -
    resumes rather than duplicates.
    """
    trunk_sid = get_or_create_tenant_trunk(garage)
    ensure_origination_url(garage, trunk_sid)
    ensure_number_associated(garage, trunk_sid, number_sid)
    return trunk_sid
