"""Per-garage configuration for automatic appointment reminders.

Same "one optional row per garage, safe to be entirely absent" shape as
``MOTReminderSettings`` / ``GarageScheduleSettings`` - a garage with no row
just means "not configured yet" (see
``app.appointment_reminders.defaults.resolve_appointment_reminder_settings``
for the in-code default that stands in for a missing row).

Unlike MOT reminders (three fixed named stages), appointment reminders support
any number of configurable timings via :class:`AppointmentReminderTiming`, so
adding a new lead time doesn't need a schema change.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class AppointmentReminderSettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "appointment_reminder_settings"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_appointment_reminder_settings_garage_id"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Channels the owner has selected, in priority order (e.g. ["sms", "email"]).
    # Actual availability is re-checked against this garage's communications
    # configuration at send time - selecting a channel here doesn't guarantee
    # it will be used (see app.appointment_reminders.service.available_channels).
    channels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    garage: Mapped["Garage"] = relationship("Garage", back_populates="appointment_reminder_settings")
    timings: Mapped[list["AppointmentReminderTiming"]] = relationship(
        "AppointmentReminderTiming",
        back_populates="settings",
        cascade="all, delete-orphan",
        order_by="desc(AppointmentReminderTiming.hours_before)",
    )


class AppointmentReminderTiming(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "appointment_reminder_timings"
    __table_args__ = (
        UniqueConstraint(
            "settings_id", "hours_before", name="uq_appointment_reminder_timing_hours"
        ),
    )

    settings_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("appointment_reminder_settings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hours_before: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    settings: Mapped["AppointmentReminderSettings"] = relationship(
        "AppointmentReminderSettings", back_populates="timings"
    )
