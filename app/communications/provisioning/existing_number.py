"""Provider-agnostic existing-number onboarding.

Platform Admin records what an operator learned about a business's existing
carrier (BT is the first, not a hard-coded special case - see
docs/EXISTING_NUMBER_VOICE_ONBOARDING.md), and this module turns that into:

* which of the three modes in ``app/communications/telephony.py`` the carrier
  actually supports (:func:`recommend_mode`),
* the Twilio side CoMaz can safely provision for itself, idempotently
  (:func:`provision`), and
* exactly what is still needed from the carrier, or from the operator, before
  the integration can go live (:func:`describe_integration`).

Nothing here invents a fourth mode, and nothing here ports a number - see the
module docstring of ``telephony.py``.

**Why bidirectional/inbound-only SIP needs Twilio resources and forwarding
does not.** SIP_BYOC calls arrive at a Twilio BYOC Trunk over SIP, so a
business choosing it needs that trunk (and the SIP Domain that identifies its
inbound traffic) to exist before a call can ever reach CoMaz. PSTN_FORWARD
calls arrive as ordinary inbound calls to a CoMaz-owned number CoMaz already
has - the "provisioning" for that path is confirming the ingress number
exists, not creating anything new at Twilio.

**Idempotency.** Every Twilio resource here is found-before-created, the same
pattern ``provisioning/voice.py::buy_number`` uses for numbers: pressing
Provision twice, or retrying after a timeout whose result was never recorded,
reuses the existing resource rather than creating a duplicate. Resources are
matched by a name deterministically derived from the garage, never by trusting
client-supplied state.
"""

from __future__ import annotations

import ipaddress
import logging

from twilio.base.exceptions import TwilioRestException

from app.communications.config import is_twilio_configured
from app.extensions import db
from app.models.garage import Garage

from .. import telephony
from . import voice as voice_provisioning
from .subaccounts import SubaccountError, get_subaccount_client

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Provider capability -> recommended CoMaz mode
# --------------------------------------------------------------------------

CAPABILITY_UNKNOWN = "UNKNOWN"
CAPABILITY_BIDIRECTIONAL_SIP = "BIDIRECTIONAL_SIP"
CAPABILITY_INBOUND_SIP_ONLY = "INBOUND_SIP_ONLY"
CAPABILITY_FORWARDING_ONLY = "FORWARDING_ONLY"
PROVIDER_CAPABILITIES = (
    CAPABILITY_UNKNOWN,
    CAPABILITY_BIDIRECTIONAL_SIP,
    CAPABILITY_INBOUND_SIP_ONLY,
    CAPABILITY_FORWARDING_ONLY,
)

#: Capability -> the telephony mode it supports. UNKNOWN recommends nothing -
#: never guess a carrier's capability from silence.
_RECOMMENDED_MODE = {
    CAPABILITY_BIDIRECTIONAL_SIP: telephony.MODE_SIP_BYOC,
    CAPABILITY_INBOUND_SIP_ONLY: telephony.MODE_SIP_BYOC,
    CAPABILITY_FORWARDING_ONLY: telephony.MODE_PSTN_FORWARD,
}

# --------------------------------------------------------------------------
# Integration status - the operator-facing readiness state
# --------------------------------------------------------------------------

STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
STATUS_READY_TO_PROVISION = "READY_TO_PROVISION"
STATUS_PROVISIONING = "PROVISIONING"
STATUS_AWAITING_CARRIER_CONFIGURATION = "AWAITING_CARRIER_CONFIGURATION"
STATUS_READY_FOR_TEST = "READY_FOR_TEST"
STATUS_ACTIVE = "ACTIVE"
STATUS_ERROR = "ERROR"
INTEGRATION_STATUSES = (
    STATUS_NOT_CONFIGURED,
    STATUS_READY_TO_PROVISION,
    STATUS_PROVISIONING,
    STATUS_AWAITING_CARRIER_CONFIGURATION,
    STATUS_READY_FOR_TEST,
    STATUS_ACTIVE,
    STATUS_ERROR,
)

STATUS_LABELS = {
    STATUS_NOT_CONFIGURED: "Not configured",
    STATUS_READY_TO_PROVISION: "Ready to provision",
    STATUS_PROVISIONING: "Provisioning",
    STATUS_AWAITING_CARRIER_CONFIGURATION: "Awaiting carrier configuration",
    STATUS_READY_FOR_TEST: "Ready for test",
    STATUS_ACTIVE: "Active",
    STATUS_ERROR: "Error",
}

# A human transfer must never be dialled through, so provisioning and
# activation both stop dead rather than guess past one.
_MAX_NOTES_LENGTH = 4000


class ExistingNumberValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


class ExistingNumberProvisioningError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def recommend_mode(capability: str | None) -> str | None:
    """The telephony mode this capability supports, or ``None`` when the
    capability is unknown/unset - CoMaz never guesses a mode from silence."""
    return _RECOMMENDED_MODE.get(capability or "")


# --------------------------------------------------------------------------
# Provider info (operator input, non-secret)
# --------------------------------------------------------------------------


def validate_provider_info(data: dict) -> dict:
    """Normalise the operator's record of what the carrier supports.
    Raises :class:`ExistingNumberValidationError` naming the offending field.
    Nothing is written here."""
    capability = str(data.get("integration_capability") or "").strip().upper() or CAPABILITY_UNKNOWN
    if capability not in PROVIDER_CAPABILITIES:
        raise ExistingNumberValidationError(
            "Choose bidirectional SIP, inbound SIP only, call forwarding only, or not known yet.",
            field="integration_capability",
        )

    provider_name = str(data.get("integration_provider_name") or "").strip()[:120] or None
    provider_product = str(data.get("integration_provider_product") or "").strip()[:120] or None

    carrier_sip_uri = str(data.get("integration_carrier_sip_uri") or "").strip()
    normalised_sip_uri = None
    if carrier_sip_uri:
        try:
            normalised_sip_uri = telephony.validate_sip_destination(
                carrier_sip_uri, field="integration_carrier_sip_uri"
            )
        except telephony.TelephonyValidationError as exc:
            raise ExistingNumberValidationError(str(exc), field=exc.field) from exc

    raw_ips = str(data.get("integration_carrier_ip_addresses") or "").strip()
    normalised_ips = None
    if raw_ips:
        addresses = [part.strip() for part in raw_ips.split(",") if part.strip()]
        for address in addresses:
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise ExistingNumberValidationError(
                    f'"{address}" is not a valid IP address. List the carrier\'s '
                    "signalling IP addresses, separated by commas.",
                    field="integration_carrier_ip_addresses",
                ) from exc
        normalised_ips = ", ".join(dict.fromkeys(addresses))  # dedupe, keep order

    notes = str(data.get("integration_notes") or "").strip()[:_MAX_NOTES_LENGTH] or None

    return {
        "integration_provider_name": provider_name,
        "integration_provider_product": provider_product,
        "integration_capability": capability,
        "integration_carrier_sip_uri": normalised_sip_uri,
        "integration_carrier_ip_addresses": normalised_ips,
        "integration_notes": notes,
    }


# --------------------------------------------------------------------------
# Read model
# --------------------------------------------------------------------------


def status_of(settings) -> str:
    return (settings.integration_status if settings else None) or STATUS_NOT_CONFIGURED


def _domain_name(garage: Garage) -> str:
    """A deterministic, globally-unique SIP Domain name for this garage -
    pure function of the garage id, so provisioning and describing never
    need an extra column to agree on it."""
    return f"comaz-{garage.id.hex[:20]}.sip.twilio.com"


def _byoc_trunk_friendly_name(garage: Garage) -> str:
    return f"CoMaz — {garage.name}"[:255]


def _connection_policy_friendly_name(garage: Garage) -> str:
    return f"CoMaz — {garage.name} carrier egress"[:255]


def _ip_acl_friendly_name(garage: Garage) -> str:
    return f"CoMaz — {garage.name} carrier"[:255]


def still_required(garage: Garage) -> list[str]:
    """What is missing before this integration can be provisioned and
    activated - shown verbatim in Platform Admin. Never guessed past."""
    settings = getattr(garage, "communication_settings", None)
    capability = settings.integration_capability if settings else None
    missing: list[str] = []

    if not capability or capability == CAPABILITY_UNKNOWN:
        missing.append(
            "The carrier's connectivity: bidirectional SIP, inbound SIP only, or call "
            "forwarding only."
        )
        return missing

    mode = recommend_mode(capability)
    public_number = settings.public_business_number if settings else None
    if not public_number:
        missing.append("The business's existing public number.")

    if mode == telephony.MODE_SIP_BYOC:
        if not (settings and settings.integration_carrier_ip_addresses):
            missing.append(
                "The carrier's SIP signalling IP address(es), to authorise inbound calls."
            )
        if capability == CAPABILITY_BIDIRECTIONAL_SIP and not (
            settings and settings.integration_carrier_sip_uri
        ):
            missing.append(
                "The carrier's own SIP endpoint, to transfer calls needing a person back "
                "to their PBX/hunt group (or record a safe phone number instead)."
            )
    elif mode == telephony.MODE_PSTN_FORWARD:
        if not (settings and settings.voice_phone_number):
            missing.append("A CoMaz number for the business's existing number to forward to.")

    if not (settings and settings.human_primary_type and settings.human_primary_destination):
        missing.append("A primary human handoff destination.")

    return missing


def configure_at_carrier(garage: Garage) -> dict | None:
    """Exactly what the operator needs to give the carrier - non-secret,
    shown after CoMaz's own side is provisioned. ``None`` before then."""
    settings = getattr(garage, "communication_settings", None)
    if settings is None or status_of(settings) == STATUS_NOT_CONFIGURED:
        return None

    # The recommendation, not the saved telephony_mode - describe_integration
    # can be called before provisioning has ever set it (provision() sets it
    # to match, so the two agree once provisioning has actually run).
    mode = recommend_mode(settings.integration_capability)
    if mode == telephony.MODE_SIP_BYOC:
        if not settings.byoc_sip_domain_sid:
            return None
        info: dict = {
            "mode": mode,
            "termination_domain": _domain_name(garage),
            "public_number_to_send": settings.public_business_number,
            "ip_authorisation": (
                f"CoMaz only accepts SIP from: {settings.integration_carrier_ip_addresses}"
                if settings.integration_carrier_ip_addresses
                else "Not yet recorded - CoMaz will refuse inbound SIP until the carrier's "
                "signalling IP address(es) are recorded here."
            ),
        }
        if settings.integration_carrier_sip_uri:
            info["outbound_transfer_target"] = (
                f"CoMaz will send human-transfer calls to: {settings.integration_carrier_sip_uri}"
            )
        return info
    if mode == telephony.MODE_PSTN_FORWARD:
        return {
            "mode": mode,
            "forward_from": settings.public_business_number,
            "forward_to": settings.voice_phone_number,
            "instruction": (
                f"Set an unconditional call-forward on {settings.public_business_number} to "
                f"{settings.voice_phone_number}."
                if settings.public_business_number and settings.voice_phone_number
                else "Both the existing public number and a CoMaz ingress number are needed "
                "before a forwarding instruction can be shown."
            ),
        }
    return None


def test_checklist(garage: Garage) -> list[str]:
    """What the operator should actually dial and listen for before
    activating - CoMaz cannot verify a real inbound call from the carrier
    itself, so this is shown rather than inferred. Only shown once
    provisioning has succeeded (see :func:`describe_integration`); the
    destinations named are the *effective* chain a call would really use,
    the same one loop prevention already checked."""
    chain = telephony.human_destinations(garage)
    checklist = [
        "Call the business's public number and confirm it reaches CoMaz's phone menu or AI.",
        "Confirm the AI can complete a normal request (e.g. book or look up an appointment).",
    ]
    if chain:
        first = chain[0]
        kind = "SIP" if first.kind == telephony.DEST_SIP_URI else "phone"
        checklist.append(
            f"Ask for a person and confirm the call transfers, by {kind}, to "
            f"{first.value} - the caller should stay on the line throughout."
        )
        if len(chain) > 1:
            second = chain[1]
            checklist.append(
                f"With the first destination not answering, confirm the call falls back to "
                f"{second.value}."
            )
    else:
        checklist.append(
            "No human destination is currently reachable - a caller asking for a person will "
            "only get a logged callback. Confirm this is genuinely acceptable before activating."
        )
    checklist.append(
        "Confirm a rejected or unanswered transfer ends the call safely, rather than looping "
        "back into CoMaz's own menu."
    )
    return checklist


def describe_integration(garage: Garage) -> dict:
    settings = getattr(garage, "communication_settings", None)
    capability = settings.integration_capability if settings else None
    status = status_of(settings)
    return {
        "integration_provider_name": settings.integration_provider_name if settings else None,
        "integration_provider_product": settings.integration_provider_product if settings else None,
        "integration_capability": capability or CAPABILITY_UNKNOWN,
        "recommended_mode": recommend_mode(capability),
        "integration_carrier_sip_uri": settings.integration_carrier_sip_uri if settings else None,
        "integration_carrier_ip_addresses": (
            settings.integration_carrier_ip_addresses if settings else None
        ),
        "integration_notes": settings.integration_notes if settings else None,
        "integration_status": status,
        "integration_status_label": STATUS_LABELS[status],
        "integration_error": settings.integration_error if settings else None,
        "still_required": still_required(garage),
        "configure_at_carrier": configure_at_carrier(garage),
        "test_checklist": (
            test_checklist(garage)
            if status
            in (STATUS_AWAITING_CARRIER_CONFIGURATION, STATUS_READY_FOR_TEST, STATUS_ACTIVE)
            else []
        ),
        "can_activate": status in (STATUS_AWAITING_CARRIER_CONFIGURATION, STATUS_READY_FOR_TEST)
        and not still_required(garage),
    }


# --------------------------------------------------------------------------
# Provisioning (Twilio side) - explicit action only, never automatic
# --------------------------------------------------------------------------


def _twilio_error(exc: TwilioRestException, fallback: str) -> ExistingNumberProvisioningError:
    return ExistingNumberProvisioningError(
        exc.msg or fallback, code=str(exc.code) if exc.code is not None else None
    )


def _set_error(garage: Garage, message: str) -> None:
    settings = garage.communication_settings
    assert settings is not None  # provision()/activate() always ensure it first
    settings.integration_status = STATUS_ERROR
    settings.integration_error = message[:_MAX_NOTES_LENGTH]
    db.session.commit()


def _find_or_create_sip_domain(client, garage: Garage) -> str:
    domain_name = _domain_name(garage)
    urls = voice_provisioning.webhook_urls()
    existing = client.sip.domains.list(limit=50)
    for domain in existing:
        if domain.domain_name == domain_name:
            client.sip.domains(domain.sid).update(voice_url=urls["voice_url"], voice_method="POST")
            return str(domain.sid)
    created = client.sip.domains.create(
        domain_name=domain_name,
        friendly_name=f"CoMaz — {garage.name}"[:64],
        voice_url=urls["voice_url"],
        voice_method="POST",
    )
    logger.info(
        "[provisioning] created SIP Domain %s (%s) for garage %s",
        domain_name,
        created.sid,
        garage.id,
    )
    return str(created.sid)


def _find_or_create_ip_acl(client, garage: Garage, ip_addresses: list[str]) -> str:
    friendly_name = _ip_acl_friendly_name(garage)
    existing = client.sip.ip_access_control_lists.list(limit=50)
    acl_sid = None
    for acl in existing:
        if acl.friendly_name == friendly_name:
            acl_sid = str(acl.sid)
            break
    if acl_sid is None:
        created = client.sip.ip_access_control_lists.create(friendly_name=friendly_name)
        acl_sid = str(created.sid)
        logger.info("[provisioning] created IP ACL %s for garage %s", acl_sid, garage.id)

    current = {
        entry.ip_address
        for entry in client.sip.ip_access_control_lists(acl_sid).ip_addresses.list()
    }
    for address in ip_addresses:
        if address in current:
            continue
        client.sip.ip_access_control_lists(acl_sid).ip_addresses.create(
            friendly_name=f"{address}"[:64], ip_address=address
        )
    return acl_sid


def _ensure_domain_acl_mapping(client, domain_sid: str, acl_sid: str) -> None:
    mapped = {
        str(mapping.sid)
        for mapping in client.sip.domains(domain_sid).ip_access_control_list_mappings.list()
    }
    if acl_sid not in mapped:
        client.sip.domains(domain_sid).ip_access_control_list_mappings.create(
            ip_access_control_list_sid=acl_sid
        )


def _find_or_create_connection_policy(client, garage: Garage, target_uri: str) -> str:
    friendly_name = _connection_policy_friendly_name(garage)
    existing = client.voice.v1.connection_policies.list(limit=50)
    policy_sid = None
    for policy in existing:
        if policy.friendly_name == friendly_name:
            policy_sid = str(policy.sid)
            break
    if policy_sid is None:
        created = client.voice.v1.connection_policies.create(friendly_name=friendly_name)
        policy_sid = str(created.sid)
        logger.info(
            "[provisioning] created Connection Policy %s for garage %s", policy_sid, garage.id
        )

    targets = client.voice.v1.connection_policies(policy_sid).connection_policy_targets.list()
    if not any(t.target == target_uri for t in targets):
        client.voice.v1.connection_policies(policy_sid).connection_policy_targets.create(
            target=target_uri, priority=10, weight=10, enabled=True
        )
    return policy_sid


def _find_or_create_byoc_trunk(
    client, garage: Garage, *, sip_domain_sid: str, connection_policy_sid: str | None
) -> str:
    friendly_name = _byoc_trunk_friendly_name(garage)
    urls = voice_provisioning.webhook_urls()
    existing = client.voice.v1.byoc_trunks.list(limit=50)
    trunk_sid = None
    for trunk in existing:
        if trunk.friendly_name == friendly_name:
            trunk_sid = str(trunk.sid)
            break

    update_kwargs: dict[str, object] = {
        "voice_url": urls["voice_url"],
        "voice_method": "POST",
        "status_callback_url": urls["status_callback"],
        "status_callback_method": "POST",
        "from_domain_sid": sip_domain_sid,
    }
    if connection_policy_sid:
        update_kwargs["connection_policy_sid"] = connection_policy_sid

    if trunk_sid is None:
        created = client.voice.v1.byoc_trunks.create(friendly_name=friendly_name, **update_kwargs)
        trunk_sid = str(created.sid)
        logger.info("[provisioning] created BYOC Trunk %s for garage %s", trunk_sid, garage.id)
    else:
        client.voice.v1.byoc_trunks(trunk_sid).update(**update_kwargs)

    return trunk_sid


def _provision_sip_byoc(garage: Garage) -> None:
    if not is_twilio_configured():
        raise ExistingNumberProvisioningError("Twilio is not configured for this deployment.")

    try:
        client = get_subaccount_client(garage)
    except SubaccountError as exc:
        raise ExistingNumberProvisioningError(str(exc)) from exc

    settings = garage.communication_settings
    assert settings is not None  # provision() always ensures it first

    # Each resource's SID is written to ``settings`` as soon as it exists -
    # not only at the end - so a failure partway through (the trunk step
    # refused, say) leaves the domain/ACL/policy it already created visible
    # rather than silently discarded; a retry's idempotent lookups find them
    # again either way, but the operator should see how far it got.
    try:
        sip_domain_sid = _find_or_create_sip_domain(client, garage)
        settings.byoc_sip_domain_sid = sip_domain_sid
        db.session.commit()

        acl_sid = None
        if settings.integration_carrier_ip_addresses:
            addresses = [
                a.strip() for a in settings.integration_carrier_ip_addresses.split(",") if a.strip()
            ]
            acl_sid = _find_or_create_ip_acl(client, garage, addresses)
            _ensure_domain_acl_mapping(client, sip_domain_sid, acl_sid)
            settings.integration_ip_acl_sid = acl_sid
            db.session.commit()

        connection_policy_sid = None
        if settings.integration_carrier_sip_uri:
            connection_policy_sid = _find_or_create_connection_policy(
                client, garage, settings.integration_carrier_sip_uri
            )
            settings.integration_connection_policy_sid = connection_policy_sid
            db.session.commit()

        trunk_sid = _find_or_create_byoc_trunk(
            client,
            garage,
            sip_domain_sid=sip_domain_sid,
            connection_policy_sid=connection_policy_sid,
        )
        settings.byoc_trunk_sid = trunk_sid
        db.session.commit()
    except TwilioRestException as exc:
        raise _twilio_error(
            exc, "Twilio refused to provision this business's SIP routing."
        ) from exc


def provision(garage: Garage) -> dict:
    """Provision CoMaz's own side of this business's existing-number
    integration, idempotently. Raises :class:`ExistingNumberProvisioningError`
    on failure, having first recorded it as ``integration_status=ERROR`` so
    Platform Admin shows the same message on reload.

    Never activates anything - see :func:`activate`. A caller (Platform
    Admin) triggers this from one explicit button; nothing here runs during a
    page load or a routine request.
    """
    settings = garage.communication_settings
    if settings is None:
        raise ExistingNumberProvisioningError(
            "Set up this business's communications before provisioning an existing-number "
            "integration."
        )

    capability = settings.integration_capability
    mode = recommend_mode(capability)
    if mode is None:
        raise ExistingNumberProvisioningError(
            "Record the carrier's capability (bidirectional SIP, inbound SIP only, or call "
            "forwarding only) before provisioning."
        )
    if settings.telephony_mode and settings.telephony_mode != mode:
        raise ExistingNumberProvisioningError(
            f"The saved telephony mode ({settings.telephony_mode}) does not match what "
            f"{capability} supports ({mode}). Update the telephony mode, or the recorded "
            "capability, before provisioning."
        )

    missing = still_required(garage)
    # A missing human destination or public number blocks provisioning too -
    # provisioning Twilio resources for a business nothing can safely route
    # to is worse than refusing outright.
    if missing:
        raise ExistingNumberProvisioningError(
            "Still needed before provisioning: " + "; ".join(missing)
        )

    settings.integration_status = STATUS_PROVISIONING
    settings.integration_error = None
    db.session.commit()

    try:
        if mode == telephony.MODE_SIP_BYOC:
            _provision_sip_byoc(garage)
        # MODE_PSTN_FORWARD needs no new Twilio resource - the business's
        # existing CoMaz ingress number is already what it forwards to.
        # CoMaz applies its own recommendation here - the whole point of
        # recording the carrier's capability is that the operator should not
        # have to separately go and pick the matching mode by hand.
        settings.telephony_mode = mode
        settings.integration_status = STATUS_AWAITING_CARRIER_CONFIGURATION
        settings.integration_error = None
        db.session.commit()
    except ExistingNumberProvisioningError as exc:
        _set_error(garage, str(exc))
        raise
    except Exception as exc:  # pragma: no cover - defensive, mirrors voice.py
        _set_error(garage, str(exc))
        raise ExistingNumberProvisioningError(
            "Provisioning failed unexpectedly - see the platform log."
        ) from exc

    return describe_integration(garage)


def activate(garage: Garage) -> dict:
    """The operator confirms the carrier is configured and the manual test
    checklist (inbound, IVR/AI, AI-to-human, fallback) has been run. CoMaz
    does not - and cannot, without a real call from the carrier - verify
    this itself; it only enforces that provisioning succeeded and a human
    destination is safely reachable."""
    settings = garage.communication_settings
    if settings is None:
        raise ExistingNumberProvisioningError("This business has no communications settings yet.")

    status = status_of(settings)
    if status not in (STATUS_AWAITING_CARRIER_CONFIGURATION, STATUS_READY_FOR_TEST):
        raise ExistingNumberProvisioningError(
            "Provision this business's existing-number integration, successfully, before "
            "activating it."
        )

    missing = still_required(garage)
    if missing:
        raise ExistingNumberProvisioningError(
            "Still needed before activating: " + "; ".join(missing)
        )

    if not telephony.has_human_destination(garage):
        raise ExistingNumberProvisioningError(
            "No human destination is currently reachable for this business - activating now "
            "would leave a caller who needs a person with nowhere to go."
        )

    settings.integration_status = STATUS_ACTIVE
    settings.integration_error = None
    db.session.commit()
    return describe_integration(garage)
