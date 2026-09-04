import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from .mixins import PrimaryKeyMixin, TimestampMixin


class PasswordResetToken(db.Model, PrimaryKeyMixin, TimestampMixin):
    """A single-use, time-limited password-reset token for one garage user.

    Only the SHA-256 hash of the token is stored; the raw token lives only in
    the reset link. Attached to the individual Employee, never to the Garage.
    """

    __tablename__ = "password_reset_tokens"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employee = relationship("Employee")
