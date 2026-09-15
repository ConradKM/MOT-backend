import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.booking_flow.section import BookingFlowSection
    from app.models.garage import Garage

# What control the customer is shown, and how the answer is validated and
# stored. DATE is the "put a calendar in this step" case - distinct from the
# appointment's own date, which the wizard always asks separately (a delivery
# date, a date of birth, a date the damage happened). MULTI_SELECT and
# CHECKBOX are the two that store into `value_list` rather than `value`.
FIELD_TYPES = (
    "TEXT",
    "TEXTAREA",
    "NUMBER",
    "SELECT",
    "MULTI_SELECT",
    "CHECKBOX",
    "DATE",
    "TIME",
    "EMAIL",
    "PHONE",
    "PHOTO",
)

#: Field types whose answer is a list rather than a single scalar.
MULTI_VALUE_FIELD_TYPES = ("MULTI_SELECT",)

#: Field types that must define `options`; a SELECT with nothing to select
#: from is a dead end for the customer.
OPTION_FIELD_TYPES = ("SELECT", "MULTI_SELECT")

# A field may additionally populate a real record, so a business that tracks
# the thing it books in keeps its records - and everything built on them, like
# MOT reminders - working, while a business that tracks nothing just collects
# answers.
#
# These name the item the customer is booking in: a car, a bike, a boat, a
# machine. A business that doesn't track one never binds a field and never
# encounters the concept. `ITEM_REFERENCE` is the identifier the business
# knows it by (a registration, a serial number, a frame number) and is the
# binding that decides whether a tracked record is created at all.
BINDING_ITEM_REFERENCE = "ITEM_REFERENCE"
BINDING_ITEM_MAKE = "ITEM_MAKE"
BINDING_ITEM_MODEL = "ITEM_MODEL"
BINDING_ITEM_YEAR = "ITEM_YEAR"
BINDING_ITEM_USAGE = "ITEM_USAGE"

FIELD_BINDINGS = (
    BINDING_ITEM_REFERENCE,
    BINDING_ITEM_MAKE,
    BINDING_ITEM_MODEL,
    BINDING_ITEM_YEAR,
    BINDING_ITEM_USAGE,
)

#: Bindings that only make sense on a numeric field.
NUMERIC_BINDINGS = (BINDING_ITEM_YEAR, BINDING_ITEM_USAGE)


class BookingFlowField(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One question inside a :class:`BookingFlowSection`."""

    __tablename__ = "booking_flow_fields"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    booking_flow_section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("booking_flow_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    help_text: Mapped[str | None] = mapped_column(String(500))
    placeholder: Mapped[str | None] = mapped_column(String(200))
    field_type: Mapped[str] = mapped_column(String(20), nullable=False, default="TEXT")
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Choices for SELECT / MULTI_SELECT. Same ARRAY(String) shape as
    # ChecklistTemplateItem.result_options.
    options: Mapped[list[str]] = mapped_column(ARRAY(String(200)), nullable=False, default=list)
    # Optional bounds, meaningful per type: NUMBER uses min/max, text types
    # use max_length. Enforced server-side at submission - the client's own
    # validation is a convenience, never the authority.
    min_value: Mapped[int | None] = mapped_column(Integer)
    max_value: Mapped[int | None] = mapped_column(Integer)
    max_length: Mapped[int | None] = mapped_column(Integer)
    # NULL = the answer is only ever stored as an answer. Non-null = it also
    # populates a real record; see FIELD_BINDINGS.
    binds_to: Mapped[str | None] = mapped_column(String(30))

    garage: Mapped["Garage"] = relationship("Garage")
    section: Mapped["BookingFlowSection"] = relationship(
        "BookingFlowSection", back_populates="fields"
    )
