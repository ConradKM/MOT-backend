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

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class GarageCommunicationSettings(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "garage_communication_settings"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_garage_communication_settings_garage_id"),
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

    garage = relationship("Garage", back_populates="communication_settings")
