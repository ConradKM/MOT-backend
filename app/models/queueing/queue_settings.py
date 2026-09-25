"""Per-garage walk-in queue configuration.

What is deliberately *not* here: a walk-in capacity. "How many customers can
this business serve at once" already exists as
``GarageScheduleSettings.capacity_per_slot`` (falling back to the active
employee count), and the booking calendar and the queue are competing for the
very same bays - so the queue reads that one number (see
app/queueing/service.py::queue_capacity). A second, separately configured
count would let the two drift apart and double-sell the same bay. What a
business *can* do is protect part of that shared capacity from public
booking, via WalkInReservedWindow.

Average service time lives here rather than being written back into
``GarageAppointmentType.default_duration_minutes``: that field sizes every
public booking slot, so an auto-calculated walk-in figure silently rewriting
it would change what the booking calendar offers. A service's own duration
remains the per-type override (see app/queueing/service.py::
resolve_service_minutes); this row only supplies the garage-wide figure used
when a walk-in hasn't picked a service.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.garage import Garage

# AUTO: median of recently completed appointments (see
# app/queueing/service.py::auto_average_minutes), falling back to the
# schedule's default_appointment_minutes until there's enough history.
# MANUAL: manual_average_minutes, as typed by the owner.
AVERAGE_MODE_AUTO = "AUTO"
AVERAGE_MODE_MANUAL = "MANUAL"
AVERAGE_MODES = (AVERAGE_MODE_AUTO, AVERAGE_MODE_MANUAL)


class GarageQueueSettings(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "garage_queue_settings"
    __table_args__ = (UniqueConstraint("garage_id", name="uq_garage_queue_settings_garage_id"),)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Staff open and close the queue at will. Closed (the default) refuses new
    # joins; people already in the line are unaffected and still get served.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    average_mode: Mapped[str] = mapped_column(String(10), nullable=False, default=AVERAGE_MODE_AUTO)
    manual_average_minutes: Mapped[int | None] = mapped_column(Integer)

    # A CALLED customer who hasn't come forward within this many minutes is
    # marked NO_SHOW and the next WAITING entry is called automatically.
    # NULL disables the auto-skip.
    no_show_timeout_minutes: Mapped[int | None] = mapped_column(Integer, default=10)

    # The service a walk-in's appointment is recorded as when they didn't
    # pick one and staff don't choose one at check-in (Appointment requires
    # a type). SET NULL: retiring the service just clears the default.
    default_appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("garage_appointment_types.id", ondelete="SET NULL")
    )

    garage: Mapped["Garage"] = relationship("Garage")
    default_appointment_type: Mapped["GarageAppointmentType | None"] = relationship(
        "GarageAppointmentType"
    )
