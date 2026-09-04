"""Per-garage MOT reminder schedule.

Three configurable stages, each an interval (days before the MOT expiry) plus
an on/off flag. A garage with no row falls back to the in-code defaults in
``app/mot_reminders/defaults.py`` (30 / 7 / 1 days), so this is safe for
existing tenants without a data backfill.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class MOTReminderSettings(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "mot_reminder_settings"
    __table_args__ = (
        UniqueConstraint("garage_id", name="uq_mot_reminder_settings_garage_id"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Stage 1 = furthest out, stage 3 = closest to expiry. Kept in that order
    # by the settings endpoint on save.
    stage1_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    stage1_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    stage2_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    stage2_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    stage3_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    stage3_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    garage = relationship("Garage", back_populates="mot_reminder_settings")
