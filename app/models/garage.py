from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.communications.garage_communication_settings import (
        GarageCommunicationSettings,
    )
    from app.models.customer import Customer
    from app.models.employee import Employee
    from app.models.garage_schedule import (
        GarageOpeningHours,
        GarageScheduleException,
        GarageScheduleSettings,
    )
    from app.models.mot_record import MOTRecord
    from app.models.mot_reminder_settings import MOTReminderSettings
    from app.models.role import Role
    from app.models.vehicle import Vehicle


class Garage(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """A tenant.

    Three identifiers, three owners:

    * ``id`` (UUID) - internal primary key. Never appears in a URL, never
      accepted from a client. Every tenant-scoped query filters on this.
    * ``slug`` - the public identifier in unauthenticated booking URLs
      (``/api/public/<slug>/...``). Generated at onboarding from the name plus
      a random suffix (see ``app/garages/slug.py``); **immutable** afterwards -
      no API accepts it and no route lets a garage user change it.
    * ``name`` - the human-facing display name. The owner may change it freely
      (``PATCH /api/garage``); doing so does **not** touch the slug.

    ``layout_variant`` is platform-controlled: it is chosen at onboarding and
    resolved through the registry in ``app/garages/layouts.py``. It is not part
    of ``GarageUpdateSchema`` - garage users cannot set it.
    """

    __tablename__ = "garages"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    # NULL -> the shared default layout. A non-null value is a key into
    # app/garages/layouts.py::LAYOUT_VARIANTS. Set only by onboarding.
    layout_variant: Mapped[str | None] = mapped_column(String(50))

    # Customer-facing business details. The authoritative source for future
    # telephone / email-reminder / SMS / booking-confirmation systems - not to
    # be hardcoded anywhere. Garage users see these read-only (GET /api/garage);
    # only the platform edits them (the onboarding CLI family).
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(500))
    postcode: Mapped[str | None] = mapped_column(String(20))
    website: Mapped[str | None] = mapped_column(String(200))

    employees: Mapped[list["Employee"]] = relationship(
        "Employee", back_populates="garage", cascade="all, delete-orphan"
    )
    customers: Mapped[list["Customer"]] = relationship(
        "Customer", back_populates="garage", cascade="all, delete-orphan"
    )
    vehicles: Mapped[list["Vehicle"]] = relationship(
        "Vehicle", back_populates="garage", cascade="all, delete-orphan"
    )
    mot_records: Mapped[list["MOTRecord"]] = relationship(
        "MOTRecord", back_populates="garage", cascade="all, delete-orphan"
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="garage", cascade="all, delete-orphan"
    )
    appointment_types: Mapped[list["GarageAppointmentType"]] = relationship(
        "GarageAppointmentType", back_populates="garage", cascade="all, delete-orphan"
    )
    roles: Mapped[list["Role"]] = relationship(
        "Role", back_populates="garage", cascade="all, delete-orphan"
    )
    mot_reminder_settings: Mapped["MOTReminderSettings | None"] = relationship(
        "MOTReminderSettings",
        back_populates="garage",
        uselist=False,
        cascade="all, delete-orphan",
    )
    schedule_settings: Mapped["GarageScheduleSettings | None"] = relationship(
        "GarageScheduleSettings",
        back_populates="garage",
        uselist=False,
        cascade="all, delete-orphan",
    )
    opening_hours: Mapped[list["GarageOpeningHours"]] = relationship(
        "GarageOpeningHours",
        back_populates="garage",
        cascade="all, delete-orphan",
    )
    schedule_exceptions: Mapped[list["GarageScheduleException"]] = relationship(
        "GarageScheduleException",
        back_populates="garage",
        cascade="all, delete-orphan",
    )
    communication_settings: Mapped["GarageCommunicationSettings | None"] = relationship(
        "GarageCommunicationSettings",
        back_populates="garage",
        uselist=False,
        cascade="all, delete-orphan",
    )
