"""A per-tenant override of one feature flag.

A row exists **only** where the platform has deliberately overridden the
tenant's plan default - the absence of a row means "whatever this tenant's
plan says" (see ``app/platform_admin/features.py``, which owns the registry of
known flags and the plan matrix). That keeps the plan the single source of
truth and makes an override visible as an override.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage


class GarageFeatureFlag(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "garage_feature_flags"
    __table_args__ = (
        UniqueConstraint("garage_id", "key", name="uq_garage_feature_flags_garage_id_key"),
    )

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A key from app/platform_admin/features.py::FEATURE_FLAGS. Validated
    # there, not by a DB enum, so adding a flag needs no migration.
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)

    garage: Mapped["Garage"] = relationship("Garage", back_populates="feature_flags")
