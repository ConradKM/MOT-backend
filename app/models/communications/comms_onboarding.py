"""The per-business communications onboarding record: where Voice and
WhatsApp setup have got to, and why they are stuck.

Deliberately a **second** table rather than more columns on
:class:`~app.models.communications.garage_communication_settings.GarageCommunicationSettings`,
which stays what it has always been: the small set of live resource
identifiers every send and every inbound webhook reads. This table is
workflow - states, blockers, provider errors, timestamps, test results - and
nothing on the hot path reads it. Keeping them apart means the onboarding
flow can grow without widening the row that tenant resolution loads on every
inbound call.

The two are kept in step in one direction only, by
``app/communications/provisioning/service.py``: when a channel reaches
ONLINE, its address is written across to ``GarageCommunicationSettings``
(``voice_phone_number`` / ``whatsapp_sender``), because that is what
``app/communications/tenant_resolution.py`` matches an inbound webhook
against. Nothing reads back the other way.

**No secrets live here.** The Twilio subaccount Auth Token is in
``twilio_subaccount_credentials`` (encrypted, never serialised by any
schema); Meta access tokens are never persisted at all - CoMaz is a Tech
Provider, so Twilio holds the Meta credential once the WABA is associated,
and the one-time codes Meta sends are passed straight through to Twilio and
never stored.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class GarageCommunicationsOnboarding(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "garage_communications_onboarding"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_garage_communications_onboarding_garage_id"),
        # Tenancy guards. A WABA or a WhatsApp sender belonging to two CoMaz
        # businesses would cross-route real customer messages, so the database
        # refuses it rather than trusting every call site to check first.
        UniqueConstraint("waba_id", name="uq_garage_communications_onboarding_waba_id"),
        UniqueConstraint(
            "whatsapp_sender_sid", name="uq_garage_communications_onboarding_sender_sid"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- Voice ----------------------------------------------------------
    # app/communications/provisioning/states.py::VOICE_STATUSES.
    voice_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="NOT_STARTED", server_default="NOT_STARTED"
    )
    # Twilio's own SID for the purchased number ("PN…"), so a later
    # reconfiguration updates the right resource instead of matching on the
    # number string.
    voice_number_sid: Mapped[str | None] = mapped_column(String(64))
    # Comma-separated Twilio capability names ("voice,SMS,MMS") exactly as the
    # provider reported them at purchase - display only.
    voice_number_capabilities: Mapped[str | None] = mapped_column(String(60))
    # True once the number's voice URL, status callback and fallback URL at
    # Twilio all point at this deployment's own webhooks.
    voice_webhooks_configured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    voice_webhooks_configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voice_last_test_call_sid: Mapped[str | None] = mapped_column(String(64))
    voice_last_test_status: Mapped[str | None] = mapped_column(String(40))
    voice_last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voice_last_error_code: Mapped[str | None] = mapped_column(String(40))
    voice_last_error_message: Mapped[str | None] = mapped_column(Text)
    voice_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- WhatsApp -------------------------------------------------------
    # app/communications/provisioning/states.py::WHATSAPP_STATUSES.
    whatsapp_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="NOT_STARTED", server_default="NOT_STARTED"
    )
    # The business's own number, E.164 and *without* the "whatsapp:" prefix -
    # that prefix belongs to the Twilio address form, which is derived when
    # the sender goes online (GarageCommunicationSettings.whatsapp_sender).
    whatsapp_number: Mapped[str | None] = mapped_column(String(20))
    # What the business told us about this number before we touched it. Set
    # when they say it is already on WhatsApp, which routes onboarding to
    # EXISTING_WHATSAPP_MIGRATION_REQUIRED instead of straight to signup.
    whatsapp_number_in_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Meta identifiers handed back by Embedded Signup. Not secrets - they are
    # account ids, the same class of value as a Twilio SID.
    waba_id: Mapped[str | None] = mapped_column(String(64))
    meta_business_id: Mapped[str | None] = mapped_column(String(64))
    meta_phone_number_id: Mapped[str | None] = mapped_column(String(64))
    # Opaque nonce handed to the browser when Embedded Signup is launched and
    # required back on completion, so a completion callback can only apply to
    # the business it was started for.
    meta_signup_state: Mapped[str | None] = mapped_column(String(64))
    meta_signup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta_signup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Twilio Messaging Senders v2: SID ("XE…"), the provider's own status
    # string, and the reasons it gave for being offline.
    whatsapp_sender_sid: Mapped[str | None] = mapped_column(String(64))
    whatsapp_sender_status: Mapped[str | None] = mapped_column(String(40))
    whatsapp_display_name: Mapped[str | None] = mapped_column(String(200))
    whatsapp_offline_reason: Mapped[str | None] = mapped_column(Text)
    whatsapp_last_status_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    whatsapp_last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    whatsapp_last_test_status: Mapped[str | None] = mapped_column(String(40))
    whatsapp_last_error_code: Mapped[str | None] = mapped_column(String(40))
    whatsapp_last_error_message: Mapped[str | None] = mapped_column(Text)
    whatsapp_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Shared ---------------------------------------------------------
    # A free-text note the operator leaves for the next operator ("owner is on
    # holiday until the 14th"). Internal to the platform, never shown to the
    # business.
    notes: Mapped[str | None] = mapped_column(Text)

    garage: Mapped["Garage"] = relationship("Garage", back_populates="communications_onboarding")


class TwilioSubaccountCredential(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """The Auth Token of one Twilio subaccount, encrypted at rest.

    Its own table, not a column on ``GarageCommunicationSettings``, for one
    reason: **no schema can leak what no schema can see**. Every Platform
    Admin response is built from marshmallow schemas over the settings and
    onboarding rows; this row is loaded only by
    ``app/communications/provisioning/subaccounts.py`` when it needs to build
    a subaccount-scoped Twilio client, and there is no serialiser for it
    anywhere in the codebase.

    ``auth_token_encrypted`` is a Fernet token produced by
    ``app/communications/secrets.py`` from ``COMMS_SECRET_KEY``. Without that
    key the ciphertext is inert, so a database dump on its own does not carry
    the ability to act as any customer's Twilio subaccount.
    """

    __tablename__ = "twilio_subaccount_credentials"
    __table_args__ = (
        UniqueConstraint("subaccount_sid", name="uq_twilio_subaccount_credentials_sid"),
    )

    subaccount_sid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    auth_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Which COMMS_SECRET_KEY encrypted this, so a future key rotation can tell
    # re-encrypted rows from stale ones without trying to decrypt them all.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
