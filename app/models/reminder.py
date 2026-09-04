"""A reminder event.

One row = one reminder that was (or is scheduled to be) sent to a customer
about a vehicle. The MOT reminder feature is the only producer today: each
automatic stage and each manual send records a row here, keyed to the MOT
expiry date it belongs to so a stage is never sent twice for the same cycle.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

# `trigger` values.
TRIGGER_AUTOMATIC = "AUTOMATIC"
TRIGGER_MANUAL = "MANUAL"

# `stage` values. STAGE_1 is furthest from expiry, STAGE_3 closest; MANUAL is
# an owner-initiated extra send.
STAGE_1 = "STAGE_1"
STAGE_2 = "STAGE_2"
STAGE_3 = "STAGE_3"
STAGE_MANUAL = "MANUAL"
AUTOMATIC_STAGES = (STAGE_1, STAGE_2, STAGE_3)

# `status` values.
STATUS_PENDING = "PENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


class Reminder(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reminders"

    garage_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("garages.id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customers.id"), nullable=False
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("vehicles.id"), nullable=False
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id")
    )
    # Set on manual sends - the employee who clicked "Send reminder".
    initiated_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )

    type: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # AUTOMATIC | MANUAL
    trigger: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TRIGGER_AUTOMATIC, server_default=TRIGGER_AUTOMATIC
    )
    # STAGE_1 | STAGE_2 | STAGE_3 | MANUAL  (null for legacy rows)
    stage: Mapped[str | None] = mapped_column(String(20))
    # The MOT expiry date this reminder is for - scopes "already sent this
    # stage" to a single expiry cycle.
    mot_expiry_date: Mapped[date | None] = mapped_column(Date)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING, nullable=False)
    # Free-text delivery outcome ("emailed jane@…", "no email on file", …).
    detail: Mapped[str | None] = mapped_column(Text)
