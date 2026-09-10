"""One "log in as this business" grant.

Impersonation never touches the owner's credentials: no password is read,
compared, reset or displayed. Instead the platform mints a *fresh, short-lived
employee access token* for the chosen employee, carrying two extra claims -
``impersonation_id`` (this row) and ``impersonated_by`` (the admin) - and no
refresh token at all. The grant is therefore:

* **short-lived** - it expires with the token
  (``PLATFORM_ADMIN_IMPERSONATION_MINUTES``, 15 by default) and cannot be
  refreshed into a longer session;
* **revocable** - setting ``revoked_at`` kills the token on its very next
  request, through the JWT blocklist loader in ``app/__init__.py``;
* **visible** - the claims are readable by the garage frontend, which shows a
  persistent impersonation banner; and
* **audited** - both the start and the revocation write a
  :class:`~app.models.platform.audit_log.PlatformAuditLog` row, with the
  reason the admin gave.

An impersonation token is still an *employee* token: it carries no
``account_type="platform_admin"`` claim, so it can never call a
``/api/platform-admin`` route.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.employee import Employee
    from app.models.garage import Garage
    from app.models.platform.admin import PlatformAdmin


class ImpersonationSession(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "platform_impersonation_sessions"
    __table_args__ = (
        Index("ix_impersonation_sessions_garage_id_created_at", "garage_id", "created_at"),
    )

    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platform_admins.id", ondelete="SET NULL"), index=True
    )
    admin_email: Mapped[str | None] = mapped_column(String(320))

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The employee whose account the admin is acting as - always an existing,
    # active account in that tenant, chosen by the platform (an OWNER by
    # default). Never created for the purpose.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )

    # Required, free text: why support needed to enter this tenant. Shown in
    # the audit trail next to the action.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- one-time handoff to the garage frontend --------------------------
    # Starting an impersonation does not hand the admin console a usable
    # garage token. It returns a single-use code, which the *garage* app
    # exchanges for the real short-lived token
    # (POST /api/auth/impersonation/exchange). Only the SHA-256 of that code
    # is stored, it dies after one use (`handoff_used_at`) and it expires in
    # about a minute - so the value that briefly travels between two origins
    # is worthless by the time it could leak from browser history.
    handoff_code_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    handoff_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platform_admins.id", ondelete="SET NULL")
    )

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    admin: Mapped["PlatformAdmin | None"] = relationship("PlatformAdmin", foreign_keys=[admin_id])
    garage: Mapped["Garage"] = relationship("Garage")
    employee: Mapped["Employee"] = relationship("Employee")

    def is_active(self, now: datetime | None = None) -> bool:
        """Not revoked and not yet expired."""
        now = now or datetime.now(UTC)
        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return self.revoked_at is None and expires_at > now
