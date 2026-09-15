"""Append-only record of what a platform admin did.

One row per sensitive action: a tenant edit, a suspension, the start or
revocation of an impersonation session, a feature-flag change, an email
resend, an admin sign-in. Written by
:func:`app.platform_admin.audit.record_audit` - never by hand - and exposed
read-only through ``GET /api/platform-admin/audit-logs``. Nothing in the API
updates or deletes a row here.

Identity is stored twice on purpose: ``admin_id``/``garage_id`` are real
foreign keys (``ON DELETE SET NULL``) for joining, and
``admin_email``/``garage_name`` are *snapshots* taken at write time. A tenant
that is later deleted, or an admin whose account is removed, must not silently
erase the history of what was done to it.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

from ..mixins import PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.garage import Garage
    from app.models.platform.admin import PlatformAdmin

# `action` values. Dotted, coarse-to-fine, and free text at the DB level (like
# CommunicationLog.status) so a new audited action never needs a migration.
ACTION_LOGIN = "platform_admin.login"
ACTION_LOGIN_FAILED = "platform_admin.login_failed"
ACTION_TENANT_CREATE = "tenant.create"
ACTION_TENANT_UPDATE = "tenant.update"
ACTION_TENANT_OWNER_INVITE = "tenant.owner_invite"
ACTION_TENANT_SERVICE_CREATE = "tenant.service.create"
ACTION_TENANT_SERVICE_UPDATE = "tenant.service.update"
ACTION_TENANT_SERVICE_DELETE = "tenant.service.delete"
ACTION_TENANT_OPENING_HOURS_UPDATE = "tenant.opening_hours.update"
ACTION_TENANT_BOOKING_SETTINGS_UPDATE = "tenant.booking_settings.update"
ACTION_TENANT_SUSPEND = "tenant.suspend"
ACTION_TENANT_REACTIVATE = "tenant.reactivate"
ACTION_TENANT_ARCHIVE = "tenant.archive"
ACTION_TENANT_UNARCHIVE = "tenant.unarchive"
ACTION_TENANT_DELETE = "tenant.delete"
ACTION_TENANT_PLAN_CHANGE = "tenant.plan_change"
ACTION_TENANT_LOGO_UPLOAD = "tenant.logo.upload"
ACTION_TENANT_LOGO_DELETE = "tenant.logo.delete"
ACTION_FEATURE_FLAG_UPDATE = "tenant.feature_flag.update"
ACTION_IMPERSONATION_START = "tenant.impersonation.start"
ACTION_IMPERSONATION_REVOKE = "tenant.impersonation.revoke"
ACTION_EMAIL_RESEND = "operations.email.resend"
# Communications provisioning (app/platform_admin/routes/communications.py).
# Each one spends money, creates a resource in a third-party account, or sends
# something to a real person, so all of them are audited - and none of them
# ever carries a credential in `details`.
ACTION_COMMS_SUBACCOUNT_CREATE = "tenant.communications.subaccount"
ACTION_COMMS_VOICE_NUMBER = "tenant.communications.voice.number"
ACTION_COMMS_VOICE_CONFIGURE = "tenant.communications.voice.configure"
ACTION_COMMS_WHATSAPP_NUMBER = "tenant.communications.whatsapp.number"
ACTION_COMMS_META_SIGNUP = "tenant.communications.whatsapp.meta_signup"
ACTION_COMMS_SENDER_REGISTER = "tenant.communications.whatsapp.sender"
ACTION_COMMS_TEST = "tenant.communications.test"
ACTION_COMMS_ENABLED_TOGGLE = "tenant.communications.enabled"
ACTION_COMMS_AUTOMATION_TOGGLE = "tenant.communications.automation"


class PlatformAuditLog(db.Model, PrimaryKeyMixin, TimestampMixin):  # type: ignore[name-defined]
    __tablename__ = "platform_audit_logs"
    __table_args__ = (
        Index("ix_platform_audit_logs_created_at", "created_at"),
        Index("ix_platform_audit_logs_garage_id_created_at", "garage_id", "created_at"),
    )

    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platform_admins.id", ondelete="SET NULL"), index=True
    )
    # Snapshot - survives the admin account being deleted.
    admin_email: Mapped[str | None] = mapped_column(String(320))

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    garage_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="SET NULL")
    )
    # Snapshot - survives the tenant being deleted.
    garage_name: Mapped[str | None] = mapped_column(String(200))

    # What was acted on, when it isn't the tenant itself: "communication_log",
    # "impersonation_session", "feature_flag", ...
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))

    # One human-readable line, already rendered for the audit table.
    summary: Mapped[str | None] = mapped_column(Text)
    # Structured detail (changed fields, before/after, reason). Never carries
    # a password, a token, or a customer's personal data - see
    # app/platform_admin/audit.py.
    details: Mapped[dict | None] = mapped_column(JSON)

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    admin: Mapped["PlatformAdmin | None"] = relationship("PlatformAdmin")
    garage: Mapped["Garage | None"] = relationship("Garage")
