"""An internal CoMaz OS staff login.

Deliberately its own table, with its own password hash and its own JWT claim
(``account_type="platform_admin"``) - not a role on ``employees``. That is the
permission boundary: there is no query that turns a garage employee into a
platform admin, and no ``/api/platform-admin`` route accepts an employee or
customer token (see ``app/platform_admin/security.py``).

There is no HTTP path to create one of these. The only way an admin account
comes into existence is the ``flask create-platform-admin`` CLI, run by
someone with shell access to the deployment.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

# Full access: tenant configuration, suspension, feature flags, email resends.
ROLE_SUPERADMIN = "SUPERADMIN"
# Support desk: read everything, and impersonate for support - but never
# change a tenant's configuration, suspend one, or flip a feature flag.
ROLE_SUPPORT = "SUPPORT"
PLATFORM_ADMIN_ROLES = (ROLE_SUPERADMIN, ROLE_SUPPORT)


class PlatformAdmin(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "platform_admins"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))

    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ROLE_SUPERADMIN, server_default=ROLE_SUPERADMIN
    )

    # A deactivated admin can't log in and every live token stops working -
    # the same contract Employee.is_active has (see the JWT blocklist loader
    # in app/__init__.py).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Tokens issued before this instant are rejected. Set on a password
    # change so the change also ends every live admin session.
    tokens_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN

    @property
    def display_name(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return name or self.email
