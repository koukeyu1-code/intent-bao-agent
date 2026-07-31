"""Preserve the immutable AI suggestion on intent observations.

Revision ID: 0002_ai_snapshot
Revises: 0001_initial
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_ai_snapshot"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "raw_intent_observations",
        sa.Column("ai_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("raw_intent_observations", "ai_snapshot")
