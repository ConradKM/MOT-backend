import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

MEDIA_TYPES = ("NONE", "PHOTO", "VIDEO", "EITHER")

# Mirrors DVSA's MOT grading, extended for day-to-day service/repair work.
# Not every business runs automotive checks (a barber's "beard trim" has no
# concept of "Dangerous"), so this is offered as one selectable *preset* for
# an item's own `result_options` - never assumed platform-wide. See
# GENERIC_RESULT_OPTIONS for the default a brand-new item gets instead.
CHECKLIST_ITEM_STATUSES = (
    "PASS",
    "ADVISORY",
    "MINOR",
    "MAJOR",
    "DANGEROUS",
    "RECTIFIED",
    "RECOMMENDED",
    "CUSTOMER_DECLINED",
    "NOT_APPLICABLE",
    "NOT_CHECKED",
)

# Default result set for a checklist item with no business-specific grading
# need - "did this get done" rather than a pass/fail severity scale. Any
# business can still switch an item to CHECKLIST_ITEM_STATUSES (or its own
# custom list) via `result_options`.
GENERIC_RESULT_OPTIONS = ("NOT_CHECKED", "DONE", "NOT_APPLICABLE")


class ChecklistTemplateItem(db.Model, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "checklist_template_items"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checklist_template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("checklist_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    # Optional extra instruction/detail beyond the label - e.g. "torque to
    # spec, see manual" - shown to staff working the checklist and, when
    # visible_to_customer is set, alongside the label on public booking too.
    description: Mapped[str | None] = mapped_column(String(500))
    is_compulsory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    # Which resulting statuses force media capture on this item. Empty means
    # media is always optional, regardless of the logged status.
    media_required_for_statuses: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, default=list
    )
    # The allowed values for this item's own logged `status` (see
    # AppointmentChecklistItem) - configurable per item so a non-automotive
    # business isn't forced into DVSA-style grading. Defaults to
    # GENERIC_RESULT_OPTIONS; a garage can opt an item into
    # CHECKLIST_ITEM_STATUSES or any custom list of its own.
    result_options: Mapped[list[str]] = mapped_column(
        ARRAY(String(30)), nullable=False, default=lambda: list(GENERIC_RESULT_OPTIONS)
    )
    # Whether this step is worth showing on the public booking page's "what's
    # included" summary. Off by default - most checklist steps are internal
    # working notes (e.g. "check customer's locking wheel nut location") that
    # would just clutter the customer's view.
    visible_to_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    garage = relationship("Garage")
    checklist_template = relationship("ChecklistTemplate", back_populates="items")
