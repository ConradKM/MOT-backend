"""Platform Admin: superadmin accounts, tenant lifecycle, audit trail

Adds the CoMaz OS operator console's own tables, and the tenant-lifecycle
columns it manages. Purely additive - no existing column changes type, no
existing row changes meaning:

* ``platform_admins`` - internal staff logins, entirely separate from
  ``employees``. Empty after this migration; accounts are created only by
  ``flask create-platform-admin``.
* ``platform_audit_logs`` - append-only record of admin actions.
* ``platform_impersonation_sessions`` - short-lived, revocable support grants.
* ``garage_feature_flags`` - per-tenant overrides of a plan's feature set.
* ``garages.status`` / ``plan`` - NOT NULL with server defaults, so every
  existing tenant lands on ``ACTIVE`` / ``STANDARD``: no behaviour changes for
  anyone until an administrator deliberately changes it. The remaining garage
  columns are nullable metadata.
* ``communication_logs.subject`` / ``retry_of_id`` - both nullable. Existing
  rows keep a null subject (Platform Admin's delivery log falls back to the
  trigger event) and a null retry link.

Revision ID: 37f7ef2e9c58
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-10 02:54:42.624064

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "37f7ef2e9c58"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_admins",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("role", sa.String(length=20), server_default="SUPERADMIN", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("tokens_valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("platform_admins", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_platform_admins_email"), ["email"], unique=True)

    op.create_table(
        "garage_feature_flags",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("garage_id", "key", name="uq_garage_feature_flags_garage_id_key"),
    )
    with op.batch_alter_table("garage_feature_flags", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_garage_feature_flags_garage_id"), ["garage_id"], unique=False
        )

    op.create_table(
        "platform_audit_logs",
        sa.Column("admin_id", sa.Uuid(), nullable=True),
        sa.Column("admin_email", sa.String(length=320), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("garage_id", sa.Uuid(), nullable=True),
        sa.Column("garage_name", sa.String(length=200), nullable=True),
        sa.Column("target_type", sa.String(length=40), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["platform_admins.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("platform_audit_logs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_platform_audit_logs_action"), ["action"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_platform_audit_logs_admin_id"), ["admin_id"], unique=False
        )
        batch_op.create_index("ix_platform_audit_logs_created_at", ["created_at"], unique=False)
        batch_op.create_index(
            "ix_platform_audit_logs_garage_id_created_at", ["garage_id", "created_at"], unique=False
        )

    op.create_table(
        "platform_impersonation_sessions",
        sa.Column("admin_id", sa.Uuid(), nullable=True),
        sa.Column("admin_email", sa.String(length=320), nullable=True),
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handoff_code_hash", sa.String(length=64), nullable=True),
        sa.Column("handoff_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_admin_id", sa.Uuid(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["platform_admins.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["revoked_by_admin_id"], ["platform_admins.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("platform_impersonation_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_impersonation_sessions_garage_id_created_at",
            ["garage_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_platform_impersonation_sessions_admin_id"), ["admin_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_platform_impersonation_sessions_garage_id"), ["garage_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_platform_impersonation_sessions_handoff_code_hash"),
            ["handoff_code_hash"],
            unique=True,
        )

    with op.batch_alter_table("communication_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("subject", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("retry_of_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_communication_logs_retry_of_id"), ["retry_of_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_communication_logs_retry_of_id",
            "communication_logs",
            ["retry_of_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False)
        )
        batch_op.add_column(
            sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("suspension_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("plan", sa.String(length=40), server_default="STANDARD", nullable=False)
        )
        batch_op.add_column(sa.Column("internal_notes", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("garages", schema=None) as batch_op:
        batch_op.drop_column("internal_notes")
        batch_op.drop_column("plan")
        batch_op.drop_column("trial_ends_at")
        batch_op.drop_column("suspension_reason")
        batch_op.drop_column("status_changed_at")
        batch_op.drop_column("status")

    with op.batch_alter_table("communication_logs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_communication_logs_retry_of_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_communication_logs_retry_of_id"))
        batch_op.drop_column("retry_of_id")
        batch_op.drop_column("subject")

    with op.batch_alter_table("platform_impersonation_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_platform_impersonation_sessions_handoff_code_hash"))
        batch_op.drop_index(batch_op.f("ix_platform_impersonation_sessions_garage_id"))
        batch_op.drop_index(batch_op.f("ix_platform_impersonation_sessions_admin_id"))
        batch_op.drop_index("ix_impersonation_sessions_garage_id_created_at")

    op.drop_table("platform_impersonation_sessions")
    with op.batch_alter_table("platform_audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_platform_audit_logs_garage_id_created_at")
        batch_op.drop_index("ix_platform_audit_logs_created_at")
        batch_op.drop_index(batch_op.f("ix_platform_audit_logs_admin_id"))
        batch_op.drop_index(batch_op.f("ix_platform_audit_logs_action"))

    op.drop_table("platform_audit_logs")
    with op.batch_alter_table("garage_feature_flags", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_garage_feature_flags_garage_id"))

    op.drop_table("garage_feature_flags")
    with op.batch_alter_table("platform_admins", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_platform_admins_email"))

    op.drop_table("platform_admins")
