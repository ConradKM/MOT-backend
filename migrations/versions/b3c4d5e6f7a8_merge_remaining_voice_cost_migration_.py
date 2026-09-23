"""Merge remaining voice-cost migration head race

PR #202 (a2b3c4d5e6f7) and a concurrent agent's own reconciliation
(f2a3b4c5d6e7) both merged the same two prior heads at almost the same
time, each unaware of the other - a race, not a design conflict. This
merges those two merges into one head; no schema change of its own.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7, f2a3b4c5d6e7
Create Date: 2026-09-24 00:57:56.728346

"""

revision = "b3c4d5e6f7a8"
down_revision = ("a2b3c4d5e6f7", "f2a3b4c5d6e7")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
