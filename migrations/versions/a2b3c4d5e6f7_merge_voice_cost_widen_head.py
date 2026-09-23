"""Merge voice-cost-widen head into the booking-recovery/voice-cost merge

PR #198 (widening voice_call_metrics cost precision) and a concurrent
booking-recovery merge migration both branched from the same earlier voice-
cost head - each was correct on its own branch, but merging both into main
left two heads. This reconciles them; no schema change of its own.

Revision ID: a2b3c4d5e6f7
Revises: d4e5f6a7b8c9, f1a2b3c4d5e6
Create Date: 2026-09-24
"""

revision = "a2b3c4d5e6f7"
down_revision = ("d4e5f6a7b8c9", "f1a2b3c4d5e6")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
