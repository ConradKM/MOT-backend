from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class Garage(db.Model, PrimaryKeyMixin, TimestampMixin):
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
    slug: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False, index=True
    )
    # NULL -> the shared default layout. A non-null value is a key into
    # app/garages/layouts.py::LAYOUT_VARIANTS. Set only by onboarding.
    layout_variant: Mapped[str | None] = mapped_column(String(50))

    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(500))

    employees = relationship(
        "Employee", back_populates="garage", cascade="all, delete-orphan"
    )
    customers = relationship(
        "Customer", back_populates="garage", cascade="all, delete-orphan"
    )
    vehicles = relationship(
        "Vehicle", back_populates="garage", cascade="all, delete-orphan"
    )
    mot_records = relationship(
        "MOTRecord", back_populates="garage", cascade="all, delete-orphan"
    )
    appointments = relationship(
        "Appointment", back_populates="garage", cascade="all, delete-orphan"
    )
    appointment_types = relationship(
        "GarageAppointmentType", back_populates="garage", cascade="all, delete-orphan"
    )
    roles = relationship(
        "Role", back_populates="garage", cascade="all, delete-orphan"
    )
    schedule_settings = relationship(
        "GarageScheduleSettings",
        back_populates="garage",
        uselist=False,
        cascade="all, delete-orphan",
    )
    opening_hours = relationship(
        "GarageOpeningHours",
        back_populates="garage",
        cascade="all, delete-orphan",
    )
    schedule_exceptions = relationship(
        "GarageScheduleException",
        back_populates="garage",
        cascade="all, delete-orphan",
    )
