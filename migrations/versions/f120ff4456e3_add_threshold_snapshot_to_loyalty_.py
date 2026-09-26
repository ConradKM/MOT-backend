"""add threshold snapshot to loyalty rewards for historical integrity

Revision ID: f120ff4456e3
Revises: fc646decf504
Create Date: 2026-09-26 10:01:50.168379

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f120ff4456e3"
down_revision = "fc646decf504"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable-then-backfill-then-NOT-NULL: this PR's own prior migration
    # (fc646decf504) could already be live with real reward rows by the
    # time this one runs, so a bare NOT NULL add would fail against any
    # existing data. Backfill from each reward's own programme's *current*
    # threshold - the best available reconstruction for rows that predate
    # this column; every reward generated from this point on gets an exact
    # snapshot at creation time (see app/loyalty/service.py).
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("threshold_at_generation", sa.Integer(), nullable=True))

    op.execute(
        """
        UPDATE loyalty_rewards
        SET threshold_at_generation = loyalty_programs.threshold
        FROM loyalty_programs
        WHERE loyalty_rewards.program_id = loyalty_programs.id
        """
    )

    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.alter_column("threshold_at_generation", nullable=False)


def downgrade():
    with op.batch_alter_table("loyalty_rewards", schema=None) as batch_op:
        batch_op.drop_column("threshold_at_generation")
