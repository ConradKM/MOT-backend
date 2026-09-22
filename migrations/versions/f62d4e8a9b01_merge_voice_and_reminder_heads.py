"""Merge voice and appointment-reminder migration heads.

Revision ID: f62d4e8a9b01
Revises: a146785a0008, d4f1a6e72b90
Create Date: 2026-09-22
"""

revision = "f62d4e8a9b01"
down_revision = ("a146785a0008", "d4f1a6e72b90")
branch_labels = None
depends_on = None


def upgrade():
    """Join independent, already-applied schema branches."""


def downgrade():
    """No schema change; Alembic follows each parent on downgrade."""
