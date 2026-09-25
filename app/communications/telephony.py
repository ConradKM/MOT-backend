"""How a business's callers reach CoMaz, and where they go when they need a
person.

CoMaz supports exactly three ways in (see
docs/EXISTING_NUMBER_VOICE_ONBOARDING.md for the decision tree):

* ``SIP_BYOC`` - the business keeps its number with its own carrier/PBX,
  which delivers calls to a Twilio BYOC Trunk over SIP. Recommended.
* ``PSTN_FORWARD`` - the business's number forwards to a CoMaz-owned ingress
  number. The fallback when the carrier can't do SIP.
* ``NEW_COMAZ_NUMBER`` - customers call a CoMaz number directly.

Number porting is deliberately not a mode: CoMaz never ports numbers.

Every mode enters through Twilio Programmable Voice (the ``/incoming``
webhook), so every human handoff is a Twilio ``<Dial>`` that CoMaz keeps
control of - it can see busy / no-answer / failure and move on to the next
destination, then to a logged callback, instead of dropping the caller.
(OpenAI's own SIP REFER is blind: nothing reports back whether anyone
answered, which is why it is only the legacy direct-trunk path's last resort.)

Loop prevention is enforced here, both when a destination is configured and
again when it is dialled: a human destination may never be a number that
routes into CoMaz - any business's CoMaz ingress number, or any business's
forwarded/BYOC public number - nor a SIP URI that points back into Twilio or
OpenAI. Dialling one would ring CoMaz again instead of a person, and possibly
another tenant.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.extensions import db
from app.models.communications.communication_log import (
    CHANNEL_VOICE,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)
from app.models.communications.garage_communication_settings import (
    GarageCommunicationSettings,
)
from app.phone import e164_from_address

from .ivr.actions import IvrValidationError, normalise_transfer_number

logger = logging.getLogger(__name__)

MODE_SIP_BYOC = "SIP_BYOC"
MODE_PSTN_FORWARD = "PSTN_FORWARD"
MODE_NEW_COMAZ_NUMBER = "NEW_COMAZ_NUMBER"
TELEPHONY_MODES = (MODE_SIP_BYOC, MODE_PSTN_FORWARD, MODE_NEW_COMAZ_NUMBER)

DEST_SIP_URI = "SIP_URI"
DEST_PSTN_NUMBER = "PSTN_NUMBER"
DESTINATION_TYPES = (DEST_SIP_URI, DEST_PSTN_NUMBER)

DEFAULT_TRANSFER_TIMEOUT_SECONDS = 25
MIN_TRANSFER_TIMEOUT_SECONDS = 10
MAX_TRANSFER_TIMEOUT_SECONDS = 60
# Never ring people indefinitely: an option's own number, then the primary
# and secondary destinations, and that is the end of the chain.
MAX_HUMAN_DESTINATIONS = 3

# The provider recorded on a human-transfer attempt's CommunicationLog row -
# one per <Dial> to a person, grouped under the call by ``call_sid`` and never
# counted as a call of its own (see app/communications/queries.py).
TRANSFER_PROVIDER = "comaz_human_transfer"
TRANSFER_DIALLING = "dialling"
TRANSFER_ENDED = "ended"
# How long an unfinished transfer can make a new inbound call from the same
# caller look like the transfer ringing CoMaz again.
LOOP_WINDOW = timedelta(minutes=10)

_BYOC_TRUNK_SID_RE = re.compile(r"^BY[0-9a-fA-F]{32}$")
_SIP_DOMAIN_SID_RE = re.compile(r"^SD[0-9a-fA-F]{32}$")
_SIP_URI_RE = re.compile(
    r"^(?P<scheme>sips?):(?P<user>[A-Za-z0-9._~+\-!$&'*=,%]{1,64})@"
    r"(?P<host>[A-Za-z0-9.\-]{1,190}|\[[0-9A-Fa-f:.]{2,45}\])(?::(?P<port>\d{1,5}))?"
    r"(?P<params>(?:;transport=(?:udp|tcp|tls))?)$",
    re.IGNORECASE,
)
MAX_SIP_URI_LENGTH = 255
# Hosts that route straight back into CoMaz's own call path: a Twilio SIP
# domain or BYOC trunk (possibly this business's own ingress, or another
# tenant's), or the OpenAI project that only accepts CoMaz's signed handoff.
_BLOCKED_SIP_HOST_SUFFIXES = (".twilio.com", "twilio.com", ".openai.com", "openai.com")


class TelephonyValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class HumanDestination:
    kind: str
    value: str
    # Where it came from ("option", "primary", "secondary", "menu_fallback",
    # "menu_option", "escalation", "fallback") - logged, never dialled.
    source: str


# --------------------------------------------------------------------------
# Modes and numbers
# --------------------------------------------------------------------------


def mode_of(settings: GarageCommunicationSettings | None) -> str:
    """A business set up before modes existed behaves as NEW_COMAZ_NUMBER."""
    if settings is None or not settings.telephony_mode:
        return MODE_NEW_COMAZ_NUMBER
    return settings.telephony_mode


def own_numbers(garage) -> set[str]:
    """This business's own numbers - its CoMaz ingress and its public
    number. A call *from* one of them is the business's line, not a
    customer, and a transfer *to* one would ring CoMaz again."""
    settings = getattr(garage, "communication_settings", None)
    if settings is None:
        return set()
    return {n for n in (settings.voice_phone_number, settings.public_business_number) if n}


def comaz_routed_numbers() -> set[str]:
    """Every number that reaches CoMaz, across all businesses: each CoMaz
    ingress number and each business's public number. A public number is
    included even for NEW_COMAZ_NUMBER (where it can only be the CoMaz number
    itself), so the rule has no mode-dependent hole."""
    rows = db.session.execute(
        select(
            GarageCommunicationSettings.voice_phone_number,
            GarageCommunicationSettings.public_business_number,
        )
    ).all()
    return {number for row in rows for number in row if number}


def caller_identity(raw_from: str | None, garage) -> str:
    """The caller's number in E.164, or "" when it is withheld, not a phone
    number, or one of this business's own numbers.

    A carrier forwarding a call can present the *forwarding line* as the
    caller instead of the real one. Treating that as a customer would make
    every forwarded caller the same "customer" to the caller-scoped AI tools
    (appointment lookup, cancellation), so it is discarded rather than
    trusted."""
    number = e164_from_address(raw_from)
    if not number or number in own_numbers(garage):
        return ""
    return number


# --------------------------------------------------------------------------
# Validation (configuration time)
# --------------------------------------------------------------------------


def validate_pstn_destination(raw: str, *, field: str) -> str:
    """A safe UK E.164 human destination that does not route into CoMaz."""
    try:
        number = normalise_transfer_number(raw, field=field)
    except IvrValidationError as exc:
        raise TelephonyValidationError(str(exc), field=field) from exc
    if number in comaz_routed_numbers():
        raise TelephonyValidationError(
            "That number rings CoMaz itself, so the caller would loop back into the "
            "phone menu. Use a phone or hunt group the team actually answers.",
            field=field,
        )
    return number


def validate_sip_destination(raw: str, *, field: str) -> str:
    """A ``sip:user@host`` URI for a PBX extension or hunt group. No custom
    headers, no credentials, and never a host that routes back into Twilio
    or OpenAI."""
    value = (raw or "").strip()
    if not value or len(value) > MAX_SIP_URI_LENGTH:
        raise TelephonyValidationError(
            "Enter a SIP address like sip:reception@pbx.example.co.uk.", field=field
        )
    match = _SIP_URI_RE.match(value)
    if match is None:
        raise TelephonyValidationError(
            "Enter a SIP address like sip:reception@pbx.example.co.uk "
            "(only ;transport= may follow it).",
            field=field,
        )
    host = match.group("host").strip("[]").lower().rstrip(".")
    if any(host == s.lstrip(".") or host.endswith(s) for s in _BLOCKED_SIP_HOST_SUFFIXES):
        raise TelephonyValidationError(
            "That SIP address routes back into CoMaz's own telephony, not to a person.",
            field=field,
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise TelephonyValidationError(
            "A SIP destination must be publicly reachable - private, loopback and "
            "link-local addresses can't be dialled from CoMaz.",
            field=field,
        )
    if address is None and host in ("localhost",):
        raise TelephonyValidationError("localhost can't be a SIP destination.", field=field)
    return value


def validate_destination(kind: str | None, raw: str | None, *, field: str) -> str:
    if kind not in DESTINATION_TYPES:
        raise TelephonyValidationError(
            "Choose whether this destination is a SIP address or a phone number.",
            field=f"{field}_type",
        )
    if kind == DEST_SIP_URI:
        return validate_sip_destination(raw or "", field=field)
    return validate_pstn_destination(raw or "", field=field)


def validate_byoc_trunk_sid(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not _BYOC_TRUNK_SID_RE.match(value):
        raise TelephonyValidationError(
            "A BYOC Trunk SID starts with BY followed by 32 hex characters.",
            field="byoc_trunk_sid",
        )
    return value


def validate_sip_domain_sid(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not _SIP_DOMAIN_SID_RE.match(value):
        raise TelephonyValidationError(
            "A SIP Domain SID starts with SD followed by 32 hex characters.",
            field="byoc_sip_domain_sid",
        )
    return value


def validate_timeout(raw) -> int | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise TelephonyValidationError(
            "Ring time must be a whole number of seconds.", field="human_transfer_timeout_seconds"
        )
    if not (MIN_TRANSFER_TIMEOUT_SECONDS <= raw <= MAX_TRANSFER_TIMEOUT_SECONDS):
        raise TelephonyValidationError(
            f"Ring time must be between {MIN_TRANSFER_TIMEOUT_SECONDS} and "
            f"{MAX_TRANSFER_TIMEOUT_SECONDS} seconds.",
            field="human_transfer_timeout_seconds",
        )
    return raw


def validate_configuration(garage, data: dict) -> dict:
    """Validate a whole telephony configuration for ``garage`` (Platform
    Admin) and return the normalised column values. Raises
    TelephonyValidationError naming the offending field; nothing is written
    here.

    The rules that make each mode actually work:

    * SIP_BYOC needs the public number the carrier will deliver and the SIP
      Domain it delivers to - without both, its calls can't be matched to
      this business (and are refused, never guessed).
    * PSTN_FORWARD needs a CoMaz ingress number already on the business and a
      *different* public number that forwards to it.
    * NEW_COMAZ_NUMBER has no separate public number: customers call the
      CoMaz number itself.
    * Both existing-number modes need a primary human destination - a caller
      who needs a person must always have somewhere to go.
    """
    from app.phone import InvalidPhoneNumberError, normalize_uk_phone

    settings = getattr(garage, "communication_settings", None)
    ingress = settings.voice_phone_number if settings else None

    mode = str(data.get("telephony_mode") or "").strip().upper()
    if mode not in TELEPHONY_MODES:
        raise TelephonyValidationError(
            "Choose SIP / BYOC, call forwarding, or a new CoMaz number.", field="telephony_mode"
        )

    raw_public = str(data.get("public_business_number") or "").strip()
    public: str | None = None
    if raw_public:
        try:
            public = normalize_uk_phone(raw_public)
        except InvalidPhoneNumberError as exc:
            raise TelephonyValidationError(str(exc), field="public_business_number") from exc

    sip_domain_sid = validate_sip_domain_sid(data.get("byoc_sip_domain_sid"))
    byoc_trunk_sid = validate_byoc_trunk_sid(data.get("byoc_trunk_sid"))

    if mode == MODE_SIP_BYOC:
        if not public:
            raise TelephonyValidationError(
                "Enter the business's existing number that its carrier will send over SIP.",
                field="public_business_number",
            )
        if not sip_domain_sid:
            raise TelephonyValidationError(
                "Enter the SIP Domain SID the carrier delivers calls to - it's how CoMaz "
                "knows a SIP call is really this business's.",
                field="byoc_sip_domain_sid",
            )
    else:
        if sip_domain_sid or byoc_trunk_sid:
            raise TelephonyValidationError(
                "BYOC trunk details only apply to SIP / BYOC.",
                field="byoc_sip_domain_sid" if sip_domain_sid else "byoc_trunk_sid",
            )

    if mode == MODE_PSTN_FORWARD:
        if not ingress:
            raise TelephonyValidationError(
                "Give this business a CoMaz number first - its existing number forwards to it.",
                field="telephony_mode",
            )
        if not public:
            raise TelephonyValidationError(
                "Enter the business's existing number that will forward to CoMaz.",
                field="public_business_number",
            )
        if public == ingress:
            raise TelephonyValidationError(
                "The existing number and the CoMaz number it forwards to must be different.",
                field="public_business_number",
            )

    if mode == MODE_NEW_COMAZ_NUMBER and public and public != ingress:
        raise TelephonyValidationError(
            "With a new CoMaz number, customers call the CoMaz number itself - leave the "
            "existing number blank (put a number the team answers under human destinations).",
            field="public_business_number",
        )

    if public:
        clash = GarageCommunicationSettings.query.filter(
            GarageCommunicationSettings.garage_id != garage.id,
            (GarageCommunicationSettings.public_business_number == public)
            | (GarageCommunicationSettings.voice_phone_number == public),
        ).first()
        if clash is not None:
            raise TelephonyValidationError(
                "That number already belongs to another CoMaz business.",
                field="public_business_number",
            )

    # The numbers this configuration itself routes into CoMaz - not yet in
    # the database, so checked explicitly as well as against every saved one.
    this_business = {n for n in (public, ingress) if n}

    def _destination(prefix: str) -> tuple[str | None, str | None]:
        kind = str(data.get(f"{prefix}_type") or "").strip().upper() or None
        raw = str(data.get(f"{prefix}_destination") or "").strip() or None
        if not kind and not raw:
            return None, None
        value = validate_destination(kind, raw, field=f"{prefix}_destination")
        if kind == DEST_PSTN_NUMBER and value in this_business:
            raise TelephonyValidationError(
                "That number rings CoMaz itself, so the caller would loop back into the "
                "phone menu. Use a phone or hunt group the team actually answers.",
                field=f"{prefix}_destination",
            )
        return kind, value

    primary_type, primary = _destination("human_primary")
    secondary_type, secondary = _destination("human_secondary")
    if secondary and not primary:
        raise TelephonyValidationError(
            "Set the primary destination before a secondary one.",
            field="human_primary_destination",
        )
    if secondary and (secondary_type, secondary.lower()) == (primary_type, (primary or "").lower()):
        raise TelephonyValidationError(
            "The secondary destination must be different from the primary.",
            field="human_secondary_destination",
        )
    if mode in (MODE_SIP_BYOC, MODE_PSTN_FORWARD) and not primary:
        raise TelephonyValidationError(
            "An existing-number business needs a primary human destination, so a caller "
            "who needs a person always has somewhere to go.",
            field="human_primary_destination",
        )

    return {
        "telephony_mode": mode,
        "public_business_number": public,
        "byoc_sip_domain_sid": sip_domain_sid,
        "byoc_trunk_sid": byoc_trunk_sid,
        "human_primary_type": primary_type,
        "human_primary_destination": primary,
        "human_secondary_type": secondary_type,
        "human_secondary_destination": secondary,
        "human_transfer_timeout_seconds": validate_timeout(
            data.get("human_transfer_timeout_seconds")
        ),
    }


def describe(garage) -> dict:
    """Platform Admin's read of a business's telephony: the saved
    configuration, and the human chain exactly as a call would dial it now
    (after loop checks) - so a skipped destination is visible, not silent."""
    from .ivr import service as ivr_service

    settings = getattr(garage, "communication_settings", None)
    ivr_settings = ivr_service.get_settings(garage.id)
    chain = human_destinations(garage, ivr_settings)
    return {
        "telephony_mode": mode_of(settings),
        "telephony_mode_configured": bool(settings and settings.telephony_mode),
        "public_business_number": settings.public_business_number if settings else None,
        "comaz_ingress_number": settings.voice_phone_number if settings else None,
        "byoc_sip_domain_sid": settings.byoc_sip_domain_sid if settings else None,
        "byoc_trunk_sid": settings.byoc_trunk_sid if settings else None,
        "human_primary_type": settings.human_primary_type if settings else None,
        "human_primary_destination": settings.human_primary_destination if settings else None,
        "human_secondary_type": settings.human_secondary_type if settings else None,
        "human_secondary_destination": settings.human_secondary_destination if settings else None,
        "human_transfer_timeout_seconds": transfer_timeout(garage),
        "phone_menu_enabled": bool(ivr_settings and ivr_settings.enabled and ivr_settings.options),
        "effective_human_chain": [
            {"kind": d.kind, "destination": d.value, "source": d.source} for d in chain
        ],
    }


# --------------------------------------------------------------------------
# The human destination chain (call time)
# --------------------------------------------------------------------------


def transfer_timeout(garage) -> int:
    settings = getattr(garage, "communication_settings", None)
    configured: int | None = settings.human_transfer_timeout_seconds if settings else None
    if not configured:
        return DEFAULT_TRANSFER_TIMEOUT_SECONDS
    return max(MIN_TRANSFER_TIMEOUT_SECONDS, min(MAX_TRANSFER_TIMEOUT_SECONDS, configured))


def pstn_byoc_trunk(garage) -> str | None:
    """The BYOC Trunk a PSTN transfer should leave through - the business's
    own carrier - for a SIP_BYOC business that has recorded one."""
    settings = getattr(garage, "communication_settings", None)
    if mode_of(settings) != MODE_SIP_BYOC or settings is None:
        return None
    return settings.byoc_trunk_sid or None


def _candidates(garage, ivr_settings, option: dict | None) -> list[HumanDestination]:
    comm = getattr(garage, "communication_settings", None)
    out: list[HumanDestination] = []
    if option and option.get("target"):
        out.append(HumanDestination(DEST_PSTN_NUMBER, str(option["target"]), "option"))
    configured = bool(comm and comm.human_primary_type and comm.human_primary_destination)
    if configured:
        assert comm is not None
        out.append(
            HumanDestination(
                str(comm.human_primary_type), str(comm.human_primary_destination), "primary"
            )
        )
        if comm.human_secondary_type and comm.human_secondary_destination:
            out.append(
                HumanDestination(
                    str(comm.human_secondary_type),
                    str(comm.human_secondary_destination),
                    "secondary",
                )
            )
    # The phone menu's own numbers, then the legacy platform numbers - the
    # order a business set up before human destinations existed has always
    # used, so nothing changes for it.
    if ivr_settings is not None:
        if ivr_settings.fallback_target:
            out.append(
                HumanDestination(DEST_PSTN_NUMBER, ivr_settings.fallback_target, "menu_fallback")
            )
        for menu_option in ivr_settings.options or []:
            if menu_option.get("action") == "HUMAN_TRANSFER" and menu_option.get("target"):
                out.append(
                    HumanDestination(DEST_PSTN_NUMBER, str(menu_option["target"]), "menu_option")
                )
                break
    if comm is not None and not configured:
        if comm.voice_escalation_number:
            out.append(
                HumanDestination(DEST_PSTN_NUMBER, comm.voice_escalation_number, "escalation")
            )
        if comm.voice_fallback_number:
            out.append(HumanDestination(DEST_PSTN_NUMBER, comm.voice_fallback_number, "fallback"))
    return out


def _dialable(destination: HumanDestination, routed: set[str]) -> bool:
    """Re-checks a saved destination at dial time - a number saved before
    these rules existed, or one that became a CoMaz number since, must never
    be dialled."""
    try:
        if destination.kind == DEST_SIP_URI:
            validate_sip_destination(destination.value, field="destination")
            return True
        if destination.kind == DEST_PSTN_NUMBER:
            number = normalise_transfer_number(destination.value, field="destination")
            return number not in routed
    except (TelephonyValidationError, IvrValidationError):
        return False
    return False


def human_destinations(
    garage, ivr_settings=None, option: dict | None = None
) -> list[HumanDestination]:
    """Where a caller who needs a person goes, in order: the menu option's
    own number (when they chose one), the configured primary and secondary
    destinations, then the menu's and the legacy platform numbers. Deduped,
    loop-checked and capped at MAX_HUMAN_DESTINATIONS. Only ever this
    business's saved configuration - never caller or AI input."""
    routed = comaz_routed_numbers()
    seen: set[tuple[str, str]] = set()
    chain: list[HumanDestination] = []
    for destination in _candidates(garage, ivr_settings, option):
        key = (destination.kind, destination.value.lower())
        if key in seen:
            continue
        seen.add(key)
        if not _dialable(destination, routed):
            logger.warning(
                "VOICE_HUMAN_DESTINATION_SKIPPED garage=%s source=%s kind=%s reason=loop_or_invalid",
                garage.id,
                destination.source,
                destination.kind,
            )
            continue
        chain.append(destination)
        if len(chain) >= MAX_HUMAN_DESTINATIONS:
            break
    return chain


def has_human_destination(garage, ivr_settings=None) -> bool:
    return bool(human_destinations(garage, ivr_settings))


def refer_uri(destination: HumanDestination) -> str:
    """The destination as a SIP REFER target (the legacy direct-trunk path)."""
    if destination.kind == DEST_SIP_URI:
        return destination.value
    return f"tel:{destination.value}"


# --------------------------------------------------------------------------
# Transfer attempts - audit rows and loop detection
# --------------------------------------------------------------------------


def _attempt_rows(garage, call_sid: str):
    return CommunicationLog.query.filter_by(
        garage_id=garage.id, external_provider=TRANSFER_PROVIDER, call_sid=call_sid
    )


def record_transfer_attempt(
    garage, *, call_sid: str, caller: str, destination: HumanDestination, index: int
) -> None:
    """One row per attempt (``index`` in the chain) of a call. Idempotent -
    a re-delivered webhook never records the same attempt twice. Never
    raises: failing to write the audit row must not stop the caller being
    put through."""
    try:
        if call_sid and (
            _attempt_rows(garage, call_sid).filter(CommunicationLog.body.like(f"{index}:%")).first()
            is not None
        ):
            return
        db.session.add(
            CommunicationLog(
                garage_id=garage.id,
                channel=CHANNEL_VOICE,
                direction=DIRECTION_OUTBOUND,
                external_provider=TRANSFER_PROVIDER,
                call_sid=call_sid or None,
                from_address=caller or None,
                to_address=destination.value,
                status=TRANSFER_DIALLING,
                trigger_event="HUMAN_TRANSFER",
                body=f"{index}:{destination.source}:{destination.kind}",
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("VOICE_TRANSFER_LOG_FAILED callSid=%s garage=%s", call_sid, garage.id)


def finish_transfer_attempt(
    garage,
    *,
    call_sid: str,
    dial_status: str,
    index: int | None = None,
    dial_call_sid: str | None = None,
    sip_response_code: str | None = None,
) -> None:
    """Record how attempt ``index`` of this call ended (the latest one still
    ringing when the TwiML predates attempt indexes). A repeat of the same
    report changes nothing."""
    try:
        query = _attempt_rows(garage, call_sid).filter(CommunicationLog.status == TRANSFER_DIALLING)
        if index is not None:
            query = query.filter(CommunicationLog.body.like(f"{index}:%"))
        row = query.order_by(CommunicationLog.created_at.desc()).first()
        if row is None:
            return
        row.status = (dial_status or "unknown")[:30]
        if sip_response_code:
            row.error_code = str(sip_response_code)[:40]
        if (
            dial_call_sid
            and not CommunicationLog.query.filter_by(external_id=dial_call_sid).first()
        ):
            row.external_id = dial_call_sid
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("VOICE_TRANSFER_LOG_FAILED callSid=%s garage=%s", call_sid, garage.id)


def close_open_transfers(call_sid: str | None) -> None:
    """The parent call has ended: nothing it was ringing is still ringing."""
    if not call_sid:
        return
    try:
        rows = CommunicationLog.query.filter_by(
            external_provider=TRANSFER_PROVIDER, call_sid=call_sid, status=TRANSFER_DIALLING
        ).all()
        for row in rows:
            row.status = TRANSFER_ENDED
        if rows:
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("VOICE_TRANSFER_LOG_FAILED callSid=%s", call_sid)


def transfer_loop_suspected(garage, caller: str, *, now: datetime | None = None) -> bool:
    """Whether a new inbound call looks like one of this business's own
    in-progress human transfers ringing CoMaz again - e.g. the destination
    forwards back to the public number. True when a transfer for this
    business is still ringing and this call comes from that transfer's
    caller (the carrier kept the original caller ID) or from the
    destination itself (the carrier presented the forwarding line)."""
    if not caller:
        return False
    since = (now or datetime.now(UTC)) - LOOP_WINDOW
    try:
        ringing = CommunicationLog.query.filter(
            CommunicationLog.garage_id == garage.id,
            CommunicationLog.external_provider == TRANSFER_PROVIDER,
            CommunicationLog.status == TRANSFER_DIALLING,
            CommunicationLog.created_at >= since,
        ).all()
    except Exception:
        db.session.rollback()
        logger.exception("VOICE_TRANSFER_LOOP_CHECK_FAILED garage=%s", garage.id)
        return False
    return any(caller in (row.from_address, row.to_address) for row in ringing)
