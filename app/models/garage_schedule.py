"""Per-garage scheduling rules that drive the public availability calendar.

None of this existed before the calendar-first booking flow: a garage had no
opening hours, no slot size, no booking window and no capacity. These three
tables add exactly that, all garage-scoped and all optional - a garage with no
rows resolves to the same in-code defaults (see
app/garages/schedule/defaults.py), so the feature is safe for existing tenants.
"""

import uuid
from datetime import date as date_type
from datetime import time

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class GarageScheduleSettings(db.Model, PrimaryKeyMixin, TimestampMixin):
    """One row per garage - the knobs for how public availability is computed."""

    __tablename__ = "garage_schedule_settings"
    __table_args__ = (
        UniqueConstraint(
            "garage_id", name="uq_garage_schedule_settings_garage_id"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Granularity of the start times offered to customers.
    slot_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    # How long a booked slot is assumed to occupy when the request has no
    # appointment type, or the type has no default_duration_minutes.
    default_appointment_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    # Earliest a customer can book, measured from "now".
    min_lead_time_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    # How far ahead the booking window extends.
    max_advance_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    # Concurrent bookings a single slot can hold. NULL => fall back to the
    # garage's active employee count (appointments need an employee, so that
    # tracks real throughput).
    capacity_per_slot: Mapped[int | None] = mapped_column(Integer)
    # When the free portion of a slot's capacity is at or below this fraction,
    # the slot / day is reported as "limited" rather than "available".
    limited_threshold_ratio: Mapped[float] = mapped_column(
        Numeric(3, 2), nullable=False, default=0.5
    )

    garage = relationship("Garage", back_populates="schedule_settings")


class GarageOpeningHours(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A garage's opening hours for one weekday (0 = Monday ... 6 = Sunday)."""

    __tablename__ = "garage_opening_hours"
    __table_args__ = (
        UniqueConstraint(
            "garage_id", "weekday", name="uq_garage_opening_hours_garage_weekday"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    opens_at: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0))
    closes_at: Mapped[time] = mapped_column(Time, nullable=False, default=time(17, 0))
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    garage = relationship("Garage", back_populates="opening_hours")


class GarageScheduleException(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A one-off override for a single date - a bank holiday closure, or
    special hours. `is_closed` true (the default) means shut all day;
    otherwise opens_at/closes_at replace that weekday's normal hours."""

    __tablename__ = "garage_schedule_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "garage_id", "date", name="uq_garage_schedule_exceptions_garage_date"
        ),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str | None] = mapped_column(String(200))

    garage = relationship("Garage", back_populates="schedule_exceptions")
