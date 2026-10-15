"""Square OAuth connection columns on garage_payment_settings.

Per-business Square merchant connection (issue: Square as a payment
provider). Tokens are stored only Fernet-encrypted (app/payments/secrets.py).

Revision ID: 4f595422b02a
Revises: c3a9e1d7b502
Create Date: 2026-10-15 17:37:23.114537

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "4f595422b02a"
down_revision = "c3a9e1d7b502"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("garage_payment_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("square_merchant_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("square_location_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("square_access_token_enc", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("square_refresh_token_enc", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("square_token_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("square_connected_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "square_status",
                sa.String(length=20),
                server_default="NOT_CONNECTED",
                nullable=False,
            )
        )
        batch_op.create_unique_constraint(
            "uq_garage_payment_settings_square_merchant_id", ["square_merchant_id"]
        )


def downgrade():
    with op.batch_alter_table("garage_payment_settings", schema=None) as batch_op:
        batch_op.drop_constraint("uq_garage_payment_settings_square_merchant_id", type_="unique")
        batch_op.drop_column("square_status")
        batch_op.drop_column("square_connected_at")
        batch_op.drop_column("square_token_expires_at")
        batch_op.drop_column("square_refresh_token_enc")
        batch_op.drop_column("square_access_token_enc")
        batch_op.drop_column("square_location_id")
        batch_op.drop_column("square_merchant_id")
