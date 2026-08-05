"""Add highlight_spec column to raw_intent_observations.

Revision ID: 0005_highlight_spec
Revises: 0004_media_pipeline
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_highlight_spec"
down_revision = "0004_media_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "raw_intent_observations" in existing:
        columns = {col["name"] for col in inspector.get_columns("raw_intent_observations")}
        if "highlight_spec" not in columns:
            op.add_column(
                "raw_intent_observations",
                sa.Column("highlight_spec", sa.Text(), nullable=True),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "raw_intent_observations" in existing:
        columns = {col["name"] for col in inspector.get_columns("raw_intent_observations")}
        if "highlight_spec" in columns:
            op.drop_column("raw_intent_observations", "highlight_spec")
