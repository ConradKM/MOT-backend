"""Feedback submitted by a business user from the Business Dashboard's Help
Centre. The database row is the source of truth; the support-team email sent
alongside it (see app/feedback/routes.py) is a best-effort notification that
can fail without losing the feedback - see app/email/__init__.py's own
docstring for why a raw send_email() call is deliberately wrapped, not
retried automatically, at the call site.

`garage_id`/`employee_id` are always derived from the authenticated session
(app/auth/utils.py::get_current_employee), never trusted from the request
body - see app/feedback/routes.py. `business_name`/`user_email` are snapshotted
at submission time so a later rename/email change on the garage or employee
doesn't rewrite historical feedback.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.employee import Employee
    from app.models.garage import Garage

TYPE_GENERAL = "GENERAL"
TYPE_SUGGESTION = "SUGGESTION"
TYPE_BUG = "BUG"
TYPE_NOT_WORKING = "NOT_WORKING"
TYPE_COMPLIMENT = "COMPLIMENT"
TYPE_OTHER = "OTHER"
FEEDBACK_TYPES = (
    TYPE_GENERAL,
    TYPE_SUGGESTION,
    TYPE_BUG,
    TYPE_NOT_WORKING,
    TYPE_COMPLIMENT,
    TYPE_OTHER,
)

PRIORITY_LOW = "LOW"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_HIGH = "HIGH"
FEEDBACK_PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH)


class Feedback(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "feedback"

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )

    # Snapshotted at submission time - see module docstring.
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    user_email: Mapped[str] = mapped_column(String(320), nullable=False)

    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TYPE_GENERAL, server_default=TYPE_GENERAL
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PRIORITY_NORMAL, server_default=PRIORITY_NORMAL
    )

    # False = active/open, requires attention. True = archived/completed.
    # Never physically deleted when archived - see app/feedback/routes.py.
    done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    garage: Mapped["Garage"] = relationship("Garage")
    employee: Mapped["Employee | None"] = relationship("Employee")
