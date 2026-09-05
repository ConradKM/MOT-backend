"""One row per inbound or outbound communication attempt, across every
channel CoMaz OS will eventually support.

Generic and tenant-scoped by design, the same way ``Reminder`` is generic
across MOT-reminder stages: ``status`` and ``trigger_event`` are free text (no
DB enum) so a new provider status or a new triggering event never needs a
migration. ``customer_id`` / ``appointment_id`` / ``booking_request_id`` are
all independently nullable - an inbound call from an unrecognised number still
gets a row (customer/appointment/request all null), and a row is never
required to have all three.

``external_id`` (the provider's own SID) is unique-but-nullable: Postgres
allows any number of NULLs in a unique column, so rows created before a
provider SID exists (there are none yet, but the column stays future-proof)
don't collide, while two rows can never claim the same real Twilio SID. That
uniqueness is also what makes status-callback handling idempotent - see
``app/communications/service.py::update_communication_status``, which updates
the one row matching a SID rather than ever inserting a second one.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

CHANNEL_VOICE = "VOICE"
CHANNEL_WHATSAPP = "WHATSAPP"
CHANNEL_SMS = "SMS"
CHANNEL_EMAIL = "EMAIL"
CHANNELS = (CHANNEL_VOICE, CHANNEL_WHATSAPP, CHANNEL_SMS, CHANNEL_EMAIL)

DIRECTION_INBOUND = "INBOUND"
DIRECTION_OUTBOUND = "OUTBOUND"
DIRECTIONS = (DIRECTION_INBOUND, DIRECTION_OUTBOUND)

# Set instead of ever calling a real provider, whenever Twilio (or that
# garage's communications) isn't configured - never silently pretend a
# message went out. See app/communications/service.py.
STATUS_SKIPPED_NOT_CONFIGURED = "SKIPPED_NOT_CONFIGURED"


class CommunicationLog(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "communication_logs"
    __table_args__ = (
        Index("ix_communication_logs_garage_id_created_at", "garage_id", "created_at"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="SET NULL")
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL")
    )
    booking_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="SET NULL")
    )

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    external_provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default="twilio", server_default="twilio"
    )
    # The provider's own id for this message/call (a Twilio MessageSid /
    # CallSid). Null for a SKIPPED_NOT_CONFIGURED row, since no provider was
    # ever contacted.
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    from_address: Mapped[str | None] = mapped_column(String(60))
    to_address: Mapped[str | None] = mapped_column(String(60))

    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Which app/communications/events.py constant (if any) produced this row -
    # e.g. "BOOKING_REQUEST_APPROVED". Null for a row created directly (an
    # inbound webhook), rather than via the event dispatcher.
    trigger_event: Mapped[str | None] = mapped_column(String(60))

    body: Mapped[str | None] = mapped_column(Text)
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)

    garage = relationship("Garage")
    customer = relationship("Customer")
    appointment = relationship("Appointment")
    booking_request = relationship("BookingRequest")
