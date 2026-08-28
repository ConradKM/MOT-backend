import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PrimaryKeyMixin:
    """Opaque, non-sequential primary key.

    Random (v4) UUIDs, not v7 - v7's leading timestamp bits would leak
    creation-time ordering, which defeats the actual goal here (stopping
    id-based enumeration and cross-tenant creation-rate inference), even
    though v7 has better index locality.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
