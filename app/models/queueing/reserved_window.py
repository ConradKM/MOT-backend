"""Time a business protects for walk-ins by withholding bays from booking.

One table covers both shapes, told apart by which column is set:

* ``weekday`` (0 = Monday ... 6 = Sunday) - recurs every week, like
  GarageOpeningHours;
* ``date`` - applies to that one day only, like GarageScheduleException.

Both kinds apply together on a day they coincide; where windows overlap, the
largest ``reserved_capacity`` wins rather than summing, so two overlapping
"keep one bay free" rules still mean one bay (see
app/public_booking/availability.py::_reserved_at).

``reserved_capacity`` is a number of bays out of the garage's shared
capacity. Public booking sees ``capacity - reserved_capacity`` during the
window; a value at or above capacity makes the whole window walk-in only.
Staff-created appointments are not restricted - an owner booking someone in
by phone is making a deliberate choice.
"""

import uuid
from datetime import date as date_type
from datetime import time

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, String, Time, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin


class WalkInReservedWindow(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "walkin_reserved_windows"
    __table_args__ = (
        CheckConstraint(
            "(weekday IS NULL) <> (date IS NULL)",
            name="ck_walkin_reserved_windows_weekday_xor_date",
        ),
        CheckConstraint("starts_at < ends_at", name="ck_walkin_reserved_windows_time_order"),
        CheckConstraint("reserved_capacity >= 1", name="ck_walkin_reserved_windows_capacity"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int | None] = mapped_column(Integer)
    date: Mapped[date_type | None] = mapped_column(Date)
    starts_at: Mapped[time] = mapped_column(Time, nullable=False)
    ends_at: Mapped[time] = mapped_column(Time, nullable=False)
    reserved_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(String(200))
