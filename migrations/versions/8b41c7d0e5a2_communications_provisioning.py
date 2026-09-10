"""Communications onboarding: per-business Twilio subaccounts, Voice and WhatsApp

Purely additive. Nothing existing changes type or meaning, and every new
column is nullable or carries a server default, so every existing garage lands
on "communications not started" - which is exactly what they are today.

* ``garage_communications_onboarding`` - the Voice and WhatsApp state
  machines, Meta/Twilio identifiers, test results and last provider errors.
  One optional row per garage.
* ``twilio_subaccount_credentials`` - one encrypted subaccount Auth Token per
  subaccount. Its own table so no serialiser can reach it by accident.
* ``garage_communication_settings`` gains ``voice_number_sid``,
  ``voice_escalation_number`` and ``voice_fallback_number`` (all nullable).
* Three unique constraints on ``garage_communication_settings``:
  ``twilio_subaccount_sid``, ``voice_phone_number`` and ``whatsapp_sender``.
  These are tenancy guards - the columns
  ``app/communications/tenant_resolution.py`` resolves an inbound webhook by,
  where a duplicate would silently route one business's calls to another.
  Postgres permits unlimited NULLs in a unique column, so the existing rows
  (all NULL) are unaffected; the constraint only bites if two businesses are
  ever given the same resource.

Revision ID: 8b41c7d0e5a2
Revises: 37f7ef2e9c58
Create Date: 2026-09-10 14:05:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8b41c7d0e5a2"
down_revision = "37f7ef2e9c58"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "garage_communications_onboarding",
        sa.Column("garage_id", sa.Uuid(), nullable=False),
        # --- Voice ---
        sa.Column(
            "voice_status",
            sa.String(length=40),
            server_default="NOT_STARTED",
            nullable=False,
        ),
        sa.Column("voice_number_sid", sa.String(length=64), nullable=True),
        sa.Column("voice_number_capabilities", sa.String(length=60), nullable=True),
        sa.Column(
            "voice_webhooks_configured",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("voice_webhooks_configured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voice_last_test_call_sid", sa.String(length=64), nullable=True),
        sa.Column("voice_last_test_status", sa.String(length=40), nullable=True),
        sa.Column("voice_last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voice_last_error_code", sa.String(length=40), nullable=True),
        sa.Column("voice_last_error_message", sa.Text(), nullable=True),
        sa.Column("voice_online_at", sa.DateTime(timezone=True), nullable=True),
        # --- WhatsApp ---
        sa.Column(
            "whatsapp_status",
            sa.String(length=40),
            server_default="NOT_STARTED",
            nullable=False,
        ),
        sa.Column("whatsapp_number", sa.String(length=20), nullable=True),
        sa.Column("whatsapp_number_in_use", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("waba_id", sa.String(length=64), nullable=True),
        sa.Column("meta_business_id", sa.String(length=64), nullable=True),
        sa.Column("meta_phone_number_id", sa.String(length=64), nullable=True),
        sa.Column("meta_signup_state", sa.String(length=64), nullable=True),
        sa.Column("meta_signup_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta_signup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_sender_sid", sa.String(length=64), nullable=True),
        sa.Column("whatsapp_sender_status", sa.String(length=40), nullable=True),
        sa.Column("whatsapp_display_name", sa.String(length=200), nullable=True),
        sa.Column("whatsapp_offline_reason", sa.Text(), nullable=True),
        sa.Column("whatsapp_last_status_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_last_test_status", sa.String(length=40), nullable=True),
        sa.Column("whatsapp_last_error_code", sa.String(length=40), nullable=True),
        sa.Column("whatsapp_last_error_message", sa.Text(), nullable=True),
        sa.Column("whatsapp_online_at", sa.DateTime(timezone=True), nullable=True),
        # --- Shared ---
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("garage_id", name="uq_garage_communications_onboarding_garage_id"),
        sa.UniqueConstraint("waba_id", name="uq_garage_communications_onboarding_waba_id"),
        sa.UniqueConstraint(
            "whatsapp_sender_sid", name="uq_garage_communications_onboarding_sender_sid"
        ),
    )
    with op.batch_alter_table("garage_communications_onboarding", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_garage_communications_onboarding_garage_id"),
            ["garage_id"],
            unique=False,
        )

    op.create_table(
        "twilio_subaccount_credentials",
        sa.Column("subaccount_sid", sa.String(length=64), nullable=False),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subaccount_sid", name="uq_twilio_subaccount_credentials_sid"),
    )
    with op.batch_alter_table("twilio_subaccount_credentials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_twilio_subaccount_credentials_subaccount_sid"),
            ["subaccount_sid"],
            unique=False,
        )

    with op.batch_alter_table("garage_communication_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("voice_number_sid", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("voice_escalation_number", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(sa.Column("voice_fallback_number", sa.String(length=20), nullable=True))
        batch_op.create_unique_constraint(
            "uq_garage_communication_settings_subaccount_sid", ["twilio_subaccount_sid"]
        )
        batch_op.create_unique_constraint(
            "uq_garage_communication_settings_voice", ["voice_phone_number"]
        )
        batch_op.create_unique_constraint(
            "uq_garage_communication_settings_whatsapp", ["whatsapp_sender"]
        )


def downgrade():
    with op.batch_alter_table("garage_communication_settings", schema=None) as batch_op:
        batch_op.drop_constraint("uq_garage_communication_settings_whatsapp", type_="unique")
        batch_op.drop_constraint("uq_garage_communication_settings_voice", type_="unique")
        batch_op.drop_constraint("uq_garage_communication_settings_subaccount_sid", type_="unique")
        batch_op.drop_column("voice_fallback_number")
        batch_op.drop_column("voice_escalation_number")
        batch_op.drop_column("voice_number_sid")

    with op.batch_alter_table("twilio_subaccount_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_twilio_subaccount_credentials_subaccount_sid"))
    op.drop_table("twilio_subaccount_credentials")

    with op.batch_alter_table("garage_communications_onboarding", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_garage_communications_onboarding_garage_id"))
    op.drop_table("garage_communications_onboarding")
