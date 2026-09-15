import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.appointments.appointment_type import GarageAppointmentType
    from app.models.garage import Garage

# How a business presents a set of services to a customer choosing one.
#
# GRID is image-led: large cards, the picture doing the selling. It suits a
# business where the customer is choosing a *look* - hair, tinting, nails -
# and cannot really judge the options from a name alone.
#
# LIST is information-led: name, price, duration and description in a row. It
# suits a business where the customer already knows what they need and the
# decision is made on facts, not pictures.
#
# Neither is a default for the platform to pick: it is a property of what the
# business sells, so it is configured per business (Garage.booking_display_mode)
# and may be overridden per group.
DISPLAY_MODES = ("GRID", "LIST")


class AppointmentTypeGroup(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """A business-defined grouping of services, for navigation.

    Entirely optional: a business with a short menu groups nothing and its
    services keep coming back ungrouped (``GarageAppointmentType.group_id`` is
    nullable, and the public payload keeps a separate top-level list for
    them). Grouping earns its place once the menu is long enough that a flat
    list stops being navigable.

    Deleting a group is deliberately *not* destructive to the services in it -
    the FK on the child side is ``ondelete="SET NULL"``, so they become
    ungrouped rather than disappearing along with their booking history.
    """

    __tablename__ = "appointment_type_groups"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # Explicit display order. Services and groups are presented in the order a
    # business chose to sell them, which is rarely alphabetical - the staff
    # list's existing order_by(name) is a listing convenience, not an opinion
    # about how customers should see them.
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL inherits the business-wide Garage.booking_display_mode. Stored as
    # nullable rather than copied at creation so that changing the business
    # default actually moves every group that never expressed a preference.
    display_mode: Mapped[str | None] = mapped_column(String(10))

    # Same "row is a pointer, not a blob" shape as Garage.logo_* and
    # ChecklistItemMedia - the bytes live in object storage and only ever
    # arrive through app/storage/images.py's presigned flow. Only ever set
    # once finalize has confirmed the object exists *and* sniffed its real
    # content type, so a group is never reported as having an image whose
    # upload never finished.
    image_storage_key: Mapped[str | None] = mapped_column(String(500))
    image_content_type: Mapped[str | None] = mapped_column(String(100))
    image_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    garage: Mapped["Garage"] = relationship("Garage", back_populates="appointment_type_groups")
    appointment_types: Mapped[list["GarageAppointmentType"]] = relationship(
        "GarageAppointmentType",
        back_populates="group",
        order_by="(GarageAppointmentType.order, GarageAppointmentType.name)",
    )
