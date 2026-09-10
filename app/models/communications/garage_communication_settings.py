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

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, Uuid
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

    garage: Mapped["Garage"] = relationship("Garage", back_populates="communication_settings")
