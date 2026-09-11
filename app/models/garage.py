from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment import Appointment
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.communications.comms_onboarding import (
        GarageCommunicationsOnboarding,
    )
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
    from app.models.platform.feature_flag import GarageFeatureFlag
    from app.models.role import Role
    from app.models.vehicle import Vehicle


# Tenant lifecycle, owned by Platform Admin (app/platform_admin) and by
# nothing else - no garage-facing route reads or writes it. ACTIVE is a paying
# / fully live tenant; TRIAL is the same access with an end date attached
# (`trial_ends_at`); SUSPENDED cuts staff off - see app/auth/routes.py::Login
# and the JWT blocklist loader in app/__init__.py. "Dormant" is deliberately
# *not* a status: it is derived from activity at read time
# (app/platform_admin/stats.py), so a tenant can never get stuck in it.
GARAGE_STATUS_ACTIVE = "ACTIVE"
GARAGE_STATUS_TRIAL = "TRIAL"
GARAGE_STATUS_SUSPENDED = "SUSPENDED"
# Reversible, like SUSPENDED, but meant to be long-lived rather than chased -
# where a churned business goes. Also blocks staff login (see
# app/auth/routes.py, the JWT blocklist loader in app/__init__.py).
GARAGE_STATUS_ARCHIVED = "ARCHIVED"
GARAGE_STATUSES = (
    GARAGE_STATUS_ACTIVE,
    GARAGE_STATUS_TRIAL,
    GARAGE_STATUS_SUSPENDED,
    GARAGE_STATUS_ARCHIVED,
)


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

    # --- business logo (see app/garages/logo.py) -----------------------------
    # The bytes live in object storage; this row holds only the reference -
    # same "row is a pointer, not a blob" shape as ChecklistItemMedia.
    # `logo_storage_key` is only set once `finalize_logo_upload` has confirmed
    # the object exists *and* sniffed its real content type - never at ticket
    # time, so a business is never reported as having a logo it hasn't
    # actually finished uploading.
    logo_storage_key: Mapped[str | None] = mapped_column(String(500))
    logo_content_type: Mapped[str | None] = mapped_column(String(100))
    logo_original_filename: Mapped[str | None] = mapped_column(String(255))
    logo_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- platform-owned tenant lifecycle (Platform Admin only) --------------
    # Not in GarageSchema / GarageDetailsUpdateSchema: a garage user can
    # neither read nor write any of these through /api/garage.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=GARAGE_STATUS_ACTIVE,
        server_default=GARAGE_STATUS_ACTIVE,
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the tenant was suspended - shown to the admin who reactivates it,
    # never to the tenant.
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    # Why the tenant was archived - separate from suspension_reason so a
    # tenant suspended and later archived doesn't lose either reason, and
    # unarchiving doesn't have to guess which one to clear.
    archive_reason: Mapped[str | None] = mapped_column(Text)
    # Only meaningful while status == TRIAL. Nothing enforces it yet; the
    # column is what a future billing job would read.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Subscription plan key (app/platform_admin/features.py::PLANS). Drives
    # the tenant's default feature set, and is the hook a future
    # subscription/revenue metric would price off.
    plan: Mapped[str] = mapped_column(
        String(40), nullable=False, default="STANDARD", server_default="STANDARD"
    )
    # Free-text support notes, internal to the platform team.
    internal_notes: Mapped[str | None] = mapped_column(Text)

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
    communications_onboarding: Mapped["GarageCommunicationsOnboarding | None"] = relationship(
        "GarageCommunicationsOnboarding",
        back_populates="garage",
        uselist=False,
        cascade="all, delete-orphan",
    )
    feature_flags: Mapped[list["GarageFeatureFlag"]] = relationship(
        "GarageFeatureFlag",
        back_populates="garage",
        cascade="all, delete-orphan",
    )
