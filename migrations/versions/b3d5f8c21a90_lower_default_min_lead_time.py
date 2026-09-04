"""Lower the default minimum booking notice from 24h to 2h

Existing garages were backfilled with the old 24-hour default, which blocks
every same-day booking. Bring rows still on that untouched default down to 2
hours so same-day booking works; a garage that wants more notice sets it in
Settings > Availability.

Revision ID: b3d5f8c21a90
Revises: a7c1e9f4d2b8
Create Date: 2026-09-04 09:00:00.000000

"""
from alembic import op

revision = 'b3d5f8c21a90'
down_revision = 'a7c1e9f4d2b8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE garage_schedule_settings SET min_lead_time_hours = 2 "
        "WHERE min_lead_time_hours = 24"
    )


def downgrade():
    op.execute(
        "UPDATE garage_schedule_settings SET min_lead_time_hours = 24 "
        "WHERE min_lead_time_hours = 2"
    )
