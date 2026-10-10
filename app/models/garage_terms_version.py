import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.employee import Employee


class GarageTermsVersion(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    """One exact wording of a business's Terms & Conditions.

    The garage row only carries the *current* text and version number (see
    Garage.terms_and_conditions); this table keeps every wording that was
    ever current, because a booking records only the version number it
    accepted (BookingRequest.terms_version / QueueEntry.terms_version) and a
    confirmation link or a later dispute needs the words themselves - a
    counter without them proves nothing.

    Rows are immutable: an edit inserts the next version, never rewrites an
    old one (enforced below, not just by convention), so what a past
    customer agreed to can't change underneath them.
    """

    __tablename__ = "garage_terms_versions"
    __table_args__ = (
        UniqueConstraint("garage_id", "version", name="uq_garage_terms_versions_garage_version"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1-based, per garage, gap-free - see app/terms/service.py::save_terms.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Who saved this wording. SET NULL so removing an employee keeps the
    # wording (which is the whole point of this table).
    created_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL")
    )

    created_by: Mapped["Employee | None"] = relationship("Employee")


@event.listens_for(GarageTermsVersion, "before_update")
def _refuse_update(mapper, connection, target):
    raise ValueError("Terms versions are immutable - save a new version instead.")
