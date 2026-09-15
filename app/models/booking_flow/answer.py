import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.booking_flow.field import BookingFlowField
    from app.models.booking_request import BookingRequest
    from app.models.garage import Garage


class BookingRequestAnswer(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One customer answer to one configured field, captured at submission.

    ``section_title`` / ``label`` / ``field_type`` are **snapshots**, copied
    at submission rather than read live through ``booking_flow_field_id``.
    This is the same choice ``AppointmentChecklistItem`` makes against its
    template, and the same reason ``BookingRequest.requested_price`` exists: a
    business that renames "Colour preference" to "Shade" - or deletes the
    field entirely - must not silently rewrite what a customer was actually
    asked six weeks ago. The FK is kept for traceability only, and is nullable
    precisely because the field it points at may be gone.
    """

    __tablename__ = "booking_request_answers"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("garages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    booking_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("booking_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Traceability only - never the source of truth for what is rendered.
    booking_flow_field_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("booking_flow_fields.id", ondelete="SET NULL"),
    )

    # Display order as the customer saw it, so the staff review screen can
    # reproduce the form's own shape rather than an arbitrary row order.
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_title: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Scalar answers. Text rather than a typed column: the value is only ever
    # rendered back to a human, and one column beats eleven mostly-null ones.
    # The typed validation happens at submission, against the field's own
    # field_type (see app/booking_flow/answers.py).
    value: Mapped[str | None] = mapped_column(Text)
    # MULTI_SELECT answers. Kept separate rather than encoded into `value` so
    # nothing has to parse a delimiter out of customer-supplied text.
    value_list: Mapped[list[str]] = mapped_column(ARRAY(String(200)), nullable=False, default=list)

    garage: Mapped["Garage"] = relationship("Garage")
    booking_request: Mapped["BookingRequest"] = relationship(
        "BookingRequest", back_populates="answers"
    )
    booking_flow_field: Mapped["BookingFlowField | None"] = relationship("BookingFlowField")
