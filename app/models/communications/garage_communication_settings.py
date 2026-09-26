"""Per-garage Twilio resource configuration.

Deliberately the same shape as ``GarageScheduleSettings`` / ``MOTReminderSettings``
- one optional row per garage, safe to be entirely absent (a garage with no row
is simply "communications not set up yet", same as ``communications_enabled=False``).

Every field here is a **non-secret resource identifier** - a Twilio subaccount
SID, phone number, sender address, messaging service SID. There is no Twilio
Auth Token column, on purpose: the platform (master account) token lives only
in ``TWILIO_AUTH_TOKEN`` (see app/communications/config.py); once real
subaccounts exist, a subaccount's own auth token must go into a secrets
manager or an encrypted column - never a plain string here (see
docs/TWILIO_SETUP.md).

Owner-facing API surfaces must never accept writes to this table - it is
platform/developer-controlled only, the same boundary ``app/garages/details.py``
already draws around business identity fields. See
``app/communications/cli.py`` for the one supported way to change these today.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class GarageCommunicationSettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "garage_communication_settings"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_garage_communication_settings_garage_id"),
        # Tenancy guards, enforced by the database rather than by every call
        # site remembering to check. Two businesses sharing a subaccount, a
        # voice number or a WhatsApp sender would cross-route real customer
        # traffic - app/communications/tenant_resolution.py resolves an
        # inbound webhook by exactly these columns, and `.first()` on a
        # duplicate would silently pick a tenant at random. Postgres allows
        # any number of NULLs in a unique column, so "not set up yet" (every
        # row today) is unaffected.
        UniqueConstraint(
            "twilio_subaccount_sid", name="uq_garage_communication_settings_subaccount_sid"
        ),
        UniqueConstraint("voice_phone_number", name="uq_garage_communication_settings_voice"),
        UniqueConstraint("whatsapp_sender", name="uq_garage_communication_settings_whatsapp"),
        # A business's existing public number is a tenant key for SIP/BYOC
        # routing (app/communications/tenant_resolution.py), so two
        # businesses can never claim the same one.
        UniqueConstraint(
            "public_business_number", name="uq_garage_communication_settings_public_number"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Master switch. False (the default for every existing/new garage) means
    # the communications service always no-ops - see
    # app/communications/service.py::_resolve_send_context.
    communications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Twilio subaccount SID this garage's numbers/senders live under. NULL
    # until CoMaz OS allocates one (see app/communications/tenant_resolution.py)
    # - every communication runs from the platform master account until then.
    twilio_subaccount_sid: Mapped[str | None] = mapped_column(String(64))
    # E.164 (e.g. "+441234567890"). The number Twilio routes inbound Voice
    # calls to for this garage, and what outbound calls are placed from.
    voice_phone_number: Mapped[str | None] = mapped_column(String(20))
    # Twilio "From" address for WhatsApp, e.g. "whatsapp:+14155238886" - stored
    # with the "whatsapp:" prefix since that's exactly what the API needs.
    whatsapp_sender: Mapped[str | None] = mapped_column(String(30))
    # Optional: a Messaging Service SID, if this garage's outbound messages are
    # routed through one (sender pools / templates) rather than a fixed number.
    messaging_service_sid: Mapped[str | None] = mapped_column(String(64))
    # Twilio's own SID ("PN…") for the voice number above. Set when Platform
    # Admin buys or assigns the number, so reconfiguring its webhooks later
    # addresses the resource directly instead of searching by number string.
    voice_number_sid: Mapped[str | None] = mapped_column(String(64))

    # Where a caller goes when the automated assistant cannot help, or when
    # the assistant is off. E.164. NULL = no human escalation configured, and
    # the inbound webhook falls back to its existing spoken message rather
    # than dialling anything (see app/communications/voice_webhooks.py).
    voice_escalation_number: Mapped[str | None] = mapped_column(String(20))
    # Where Twilio should send the call if this deployment's own webhook is
    # unreachable or errors - the last line of defence, so an outage rings a
    # real phone instead of dropping the call. E.164, NULL = none.
    voice_fallback_number: Mapped[str | None] = mapped_column(String(20))

    # How callers reach CoMaz for this business - see
    # app/communications/telephony.py and docs/EXISTING_NUMBER_VOICE_ONBOARDING.md.
    # SIP_BYOC / PSTN_FORWARD / NEW_COMAZ_NUMBER; NULL = set up before modes
    # existed, which behaves exactly like NEW_COMAZ_NUMBER.
    telephony_mode: Mapped[str | None] = mapped_column(String(20))
    # The number the business's customers know, E.164. Separate from
    # ``voice_phone_number`` (CoMaz's own ingress number): under PSTN_FORWARD
    # this forwards *to* the ingress number, and under SIP_BYOC the carrier
    # keeps it and delivers calls for it over SIP, so it is also the tenant
    # key for those calls.
    public_business_number: Mapped[str | None] = mapped_column(String(20))
    # SIP_BYOC only. The BYOC Trunk ("BY...") used to send a PSTN human
    # transfer back out through the business's own carrier, and the SIP
    # Domain ("SD...") its carrier delivers inbound calls to - an inbound
    # SIP call is only accepted for this business when it arrived on this
    # domain. Neither is a secret.
    byoc_trunk_sid: Mapped[str | None] = mapped_column(String(64))
    byoc_sip_domain_sid: Mapped[str | None] = mapped_column(String(64))
    # Where a caller who needs a person goes, in order. Type is SIP_URI (a
    # PBX hunt group/extension - preferred for SIP_BYOC) or PSTN_NUMBER.
    # NULL primary = the legacy escalation/fallback numbers above are used.
    human_primary_type: Mapped[str | None] = mapped_column(String(20))
    human_primary_destination: Mapped[str | None] = mapped_column(String(255))
    human_secondary_type: Mapped[str | None] = mapped_column(String(20))
    human_secondary_destination: Mapped[str | None] = mapped_column(String(255))
    # Ring time per human destination before moving on. NULL = default.
    human_transfer_timeout_seconds: Mapped[int | None] = mapped_column(Integer)

    # Provider-agnostic existing-number onboarding - see
    # app/communications/provisioning/existing_number.py. What the operator
    # was told about the business's carrier, before CoMaz decides anything.
    # Non-secret only: a carrier auth *password* has nowhere safe to live on
    # this table (see the module docstring above) and is never captured here.
    integration_provider_name: Mapped[str | None] = mapped_column(String(120))
    integration_provider_product: Mapped[str | None] = mapped_column(String(120))
    # UNKNOWN / BIDIRECTIONAL_SIP / INBOUND_SIP_ONLY / FORWARDING_ONLY. NULL
    # behaves as UNKNOWN - nothing may be provisioned or activated from it.
    integration_capability: Mapped[str | None] = mapped_column(String(30))
    # The carrier's own SIP endpoint that CoMaz should send outbound (human
    # transfer) calls to via a Twilio Connection Policy Target - e.g.
    # "sip:trunk.provider.example". NULL = not yet known, or the carrier
    # can't take inbound SIP from CoMaz (fall back to a PSTN human number).
    integration_carrier_sip_uri: Mapped[str | None] = mapped_column(String(255))
    # Comma-separated IPv4/IPv6 addresses the carrier signals SIP INVITEs
    # from, used to build the SIP Domain's IP Access Control List. Not a
    # secret - it identifies a network, not a credential.
    integration_carrier_ip_addresses: Mapped[str | None] = mapped_column(String(500))
    # Free-form operator notes - product quirks, contact details, ticket
    # numbers. Never a place for a password or API key.
    integration_notes: Mapped[str | None] = mapped_column(Text)
    # NOT_CONFIGURED / READY_TO_PROVISION / PROVISIONING /
    # AWAITING_CARRIER_CONFIGURATION / READY_FOR_TEST / ACTIVE / ERROR. See
    # app/communications/provisioning/existing_number.py::INTEGRATION_STATUSES.
    # NULL behaves as NOT_CONFIGURED.
    integration_status: Mapped[str | None] = mapped_column(String(30))
    # The most recent provisioning failure, shown to the operator verbatim
    # rather than a generic "something went wrong". Cleared on the next
    # successful provisioning attempt.
    integration_error: Mapped[str | None] = mapped_column(Text)
    # The Connection Policy ("NY…") and IP Access Control List ("AL…") Twilio
    # resources CoMaz provisioned for this integration - non-secret resource
    # references, the same category as byoc_trunk_sid/byoc_sip_domain_sid
    # above.
    integration_connection_policy_sid: Mapped[str | None] = mapped_column(String(64))
    integration_ip_acl_sid: Mapped[str | None] = mapped_column(String(64))

    garage: Mapped["Garage"] = relationship("Garage", back_populates="communication_settings")
