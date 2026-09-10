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
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.booking_request import BookingRequest
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.garage import Garage

CHANNEL_VOICE = "VOICE"
CHANNEL_WHATSAPP = "WHATSAPP"
CHANNEL_SMS = "SMS"
CHANNEL_EMAIL = "EMAIL"
CHANNELS = (CHANNEL_VOICE, CHANNEL_WHATSAPP, CHANNEL_SMS, CHANNEL_EMAIL)

DIRECTION_INBOUND = "INBOUND"
DIRECTION_OUTBOUND = "OUTBOUND"
# A row the conversation engine writes about itself, not a message to/from
# the customer - e.g. "Booking request #1234 created" (see
# app/conversation/engine.py). Shows up in the same timeline as the
# INBOUND/OUTBOUND turns around it so staff can see what automation did.
DIRECTION_SYSTEM = "SYSTEM"
DIRECTIONS = (DIRECTION_INBOUND, DIRECTION_OUTBOUND, DIRECTION_SYSTEM)

# Set instead of ever calling a real provider, whenever Twilio (or that
# garage's communications) isn't configured - never silently pretend a
# message went out. See app/communications/service.py.
STATUS_SKIPPED_NOT_CONFIGURED = "SKIPPED_NOT_CONFIGURED"


class CommunicationLog(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "communication_logs"
    __table_args__ = (
        Index("ix_communication_logs_garage_id_created_at", "garage_id", "created_at"),
        Index("ix_communication_logs_garage_id_call_sid", "garage_id", "call_sid"),
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
    # The staff member who placed this, for a browser (Voice SDK) outbound
    # call. Null for everything else - inbound calls, WhatsApp, automation.
    initiated_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL"), index=True
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

    # The Twilio CallSid this row belongs to, when it belongs to a phone
    # call. Set on the one call-level row (from voice_webhooks.py::
    # incoming_call) *and* on every conversation-engine transcript turn for
    # that call, so a call's turns can be grouped under it without ever
    # counting each turn as its own call (see app/communications/queries.py).
    # Null for WhatsApp/SMS/email and for calls that predate this column.
    call_sid: Mapped[str | None] = mapped_column(String(40), index=True)

    # 320, not the original 60, since this column now also carries email
    # addresses (channel=EMAIL) - the same width as Customer.email, the
    # RFC 5321 maximum. Phone numbers and "whatsapp:+..." senders are far
    # shorter, so this is a pure widening with no effect on other channels.
    from_address: Mapped[str | None] = mapped_column(String(320))
    to_address: Mapped[str | None] = mapped_column(String(320))

    # The rendered subject line, for channels that have one (EMAIL). Null for
    # voice/WhatsApp/SMS, and for email rows written before this column
    # existed - Platform Admin's delivery log falls back to the trigger event.
    subject: Mapped[str | None] = mapped_column(String(300))

    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Which app/communications/events.py constant (if any) produced this row -
    # e.g. "BOOKING_REQUEST_APPROVED". Null for a row created directly (an
    # inbound webhook), rather than via the event dispatcher.
    trigger_event: Mapped[str | None] = mapped_column(String(60))
    # The app/conversation/intents.py constant detected for an INBOUND
    # message, e.g. "CREATE_BOOKING". Null for OUTBOUND/SYSTEM rows and for
    # anything logged before the conversation engine existed.
    intent: Mapped[str | None] = mapped_column(String(40))

    body: Mapped[str | None] = mapped_column(Text)
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)

    # When staff marked this INBOUND row read (see app/communications/queries.py
    # ::mark_conversation_read). Null forever for OUTBOUND rows - only an
    # inbound message/call is ever "unread".
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set on a row created by Platform Admin resending a failed message
    # (app/platform_admin/operations.py): it points at the original failure,
    # which is left untouched. Null on every ordinary row. Self-referential
    # and SET NULL, so pruning old logs can never orphan a retry.
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("communication_logs.id", ondelete="SET NULL"), index=True
    )

    garage: Mapped["Garage"] = relationship("Garage")
    retry_of: Mapped["CommunicationLog | None"] = relationship(
        "CommunicationLog", remote_side="CommunicationLog.id"
    )
    customer: Mapped["Customer | None"] = relationship("Customer")
    appointment: Mapped["Appointment | None"] = relationship("Appointment")
    booking_request: Mapped["BookingRequest | None"] = relationship("BookingRequest")
    initiated_by: Mapped["Employee | None"] = relationship("Employee")
