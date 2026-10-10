import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.employee import Employee
    from app.models.garage import Garage

# Whose appointments a feed covers. ME is always just the creator; ALL is every
# worker in the garage; SELECTED is the explicit employee list below. Only an
# OWNER may hold anything wider than ME (see app/calendar_export/service.py).
CALENDAR_FEED_SCOPES = ("ME", "ALL", "SELECTED")

# Join tables (the employee_roles pattern) rather than array columns, so a
# deleted employee or appointment type drops out of every feed's selection by
# FK cascade instead of leaving a dangling id behind. Removal only ever
# narrows a feed: an empty SELECTED list serves no events, and the
# appointment-type list is only consulted when all_appointment_types is false.
calendar_feed_employees = db.Table(
    "calendar_feed_employees",
    db.Column(
        "calendar_feed_id",
        Uuid,
        ForeignKey("calendar_feeds.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "employee_id", Uuid, ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    ),
)

calendar_feed_appointment_types = db.Table(
    "calendar_feed_appointment_types",
    db.Column(
        "calendar_feed_id",
        Uuid,
        ForeignKey("calendar_feeds.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "appointment_type_id",
        Uuid,
        ForeignKey("garage_appointment_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class CalendarFeed(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """A private, read-only iCalendar subscription URL for appointments.

    Only the SHA-256 of the URL's token is stored (the app/auth/reset.py
    pattern): the raw URL is shown once, at creation or regeneration. The
    garage a fetch serves comes from this row and nothing else, and the
    creator's authority is re-checked on every fetch, so a feed never outlives
    the permissions it was created under.
    """

    __tablename__ = "calendar_feeds"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    all_appointment_types: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Rolling window around "today" in the garage's timezone - a feed has no
    # fixed end, so it is re-evaluated on every fetch.
    days_back: Mapped[int] = mapped_column(Integer, nullable=False)
    days_forward: Mapped[int] = mapped_column(Integer, nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage: Mapped["Garage"] = relationship("Garage")
    created_by: Mapped["Employee"] = relationship("Employee")
    employees: Mapped[list["Employee"]] = relationship(
        "Employee", secondary=calendar_feed_employees
    )
    appointment_types: Mapped[list["GarageAppointmentType"]] = relationship(
        "GarageAppointmentType", secondary=calendar_feed_appointment_types
    )
