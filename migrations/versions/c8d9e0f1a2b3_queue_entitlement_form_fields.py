"""Configurable standard fields for the walk-in join form.

Revision ID: c8d9e0f1a2b3
Revises: b5c6d7e8f9a0
"""

import sqlalchemy as sa
from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garage_queue_settings") as batch:
        batch.add_column(sa.Column("collect_name", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("name_required", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("collect_phone", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("phone_required", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("collect_email", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("email_required", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("collect_vehicle_registration", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("vehicle_registration_required", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("queue_entries") as batch:
        batch.alter_column("customer_phone", existing_type=sa.String(length=40), nullable=True)
        batch.add_column(sa.Column("customer_email", sa.String(length=320), nullable=True))


def downgrade():
    with op.batch_alter_table("queue_entries") as batch:
        batch.drop_column("customer_email")
        batch.alter_column("customer_phone", existing_type=sa.String(length=40), nullable=False)
    with op.batch_alter_table("garage_queue_settings") as batch:
        for column in ("vehicle_registration_required", "collect_vehicle_registration", "email_required", "collect_email", "phone_required", "collect_phone", "name_required", "collect_name"):
            batch.drop_column(column)
