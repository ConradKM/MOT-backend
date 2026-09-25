"""One customer's place in a business's live walk-in queue.

A walk-in is deliberately its own record rather than a repurposed
``BookingRequest``: a booking request asks for a *future slot* and is approved
into an appointment, whereas a queue entry has no slot at all - its position
and estimated start are derived live (see app/queueing/eta.py) from everyone
ahead of it and from the appointments already consuming the same capacity.

Lifecycle::

    WAITING -> CALLED -> IN_SERVICE -> DONE
       |          |
       +----------+--> CANCELLED / NO_SHOW   (terminal side-exits)

``CALLED`` sits between waiting and service because a called customer may not
come forward - that is what the no-show auto-skip timeout measures (see
GarageQueueSettings.no_show_timeout_minutes). A real ``Appointment`` is only
created on the move into ``IN_SERVICE``, i.e. once the customer has actually
turned up, so nobody who never shows leaves an appointment row behind.
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
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
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.garage import Garage

QUEUE_WAITING = "WAITING"
QUEUE_CALLED = "CALLED"
QUEUE_IN_SERVICE = "IN_SERVICE"
QUEUE_DONE = "DONE"
QUEUE_CANCELLED = "CANCELLED"
QUEUE_NO_SHOW = "NO_SHOW"

QUEUE_STATUSES = (
    QUEUE_WAITING,
    QUEUE_CALLED,
    QUEUE_IN_SERVICE,
    QUEUE_DONE,
    QUEUE_CANCELLED,
    QUEUE_NO_SHOW,
)
# Still in the line - these are the entries the ETA simulation places.
QUEUE_ACTIVE_STATUSES = (QUEUE_WAITING, QUEUE_CALLED, QUEUE_IN_SERVICE)
QUEUE_TERMINAL_STATUSES = (QUEUE_DONE, QUEUE_CANCELLED, QUEUE_NO_SHOW)

# Why an entry left the line without being served - free strings, like
# CommunicationLog.status, so adding one needs no migration.
END_CUSTOMER_CANCELLED = "CUSTOMER_CANCELLED"
END_STAFF_CANCELLED = "STAFF_CANCELLED"
END_STAFF_NO_SHOW = "STAFF_NO_SHOW"
END_CALL_TIMEOUT = "CALL_TIMEOUT"
# The business day the entry joined on has ended with the entry still waiting.
END_DAY_ENDED = "DAY_ENDED"


class QueueEntry(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "queue_entries"
    __table_args__ = (
        UniqueConstraint(
            "garage_id",
            "service_date",
            "ticket_number",
            name="uq_queue_entries_garage_date_ticket",
        ),
        UniqueConstraint("public_token_hash", name="uq_queue_entries_public_token_hash"),
        Index("ix_queue_entries_garage_date_status", "garage_id", "service_date", "status"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=QUEUE_WAITING)
    end_reason: Mapped[str | None] = mapped_column(String(30))

    # The business-local day this entry queues for. A queue is a same-day
    # thing: yesterday's leftovers are swept (END_DAY_ENDED), never carried.
    service_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    # Short, human number staff call out ("number 7, please") - unique per
    # garage per day, allocated under the garage row lock.
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Queue order. Starts as join order; staff reorder rewrites it. Only
    # meaningful among WAITING entries.
    sort_key: Mapped[int] = mapped_column(Integer, nullable=False)

    customer_first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_last_name: Mapped[str | None] = mapped_column(String(100))
    # E.164, normalised by the join schema.
    customer_phone: Mapped[str] = mapped_column(String(40), nullable=False)
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vehicle_registration: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)

    # Optional - a walk-in may not know what they need yet. SET NULL so
    # retiring a service never deletes queue history.
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("garage_appointment_types.id", ondelete="SET NULL"), index=True
    )
    # Snapshot of the duration assumed at join (the chosen service's own
    # duration, else the garage's walk-in average) - same reasoning as
    # BookingRequest.requested_duration_minutes: a later settings or
    # catalogue edit must not silently reshuffle everyone's estimate.
    service_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # sha256 of the customer's bearer token. The raw token is only ever
    # returned once, in the join response, and is what the status link holds.
    public_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Guards against a second "you're up" text if an entry is re-called.
    called_sms_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set on promotion to IN_SERVICE. SET NULL so deleting the appointment
    # (a staff clean-up) doesn't erase the fact the customer queued.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL"), index=True
    )

    garage: Mapped["Garage"] = relationship("Garage")
    appointment_type: Mapped["GarageAppointmentType | None"] = relationship("GarageAppointmentType")
    appointment: Mapped["Appointment | None"] = relationship("Appointment")
