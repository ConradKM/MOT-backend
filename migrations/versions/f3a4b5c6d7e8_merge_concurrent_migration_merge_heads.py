"""Merge the concurrent no-op migration merge revisions.

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7, a2b3c4d5e6f7
Create Date: 2026-09-24
"""

revision = "f3a4b5c6d7e8"
down_revision = ("f2a3b4c5d6e7", "a2b3c4d5e6f7")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
