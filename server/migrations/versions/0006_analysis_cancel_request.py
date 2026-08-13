"""Persist cooperative cancellation requests for analysis runs.

Revision ID: 0006_analysis_cancel
Revises: 0005_highlight_spec
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_analysis_cancel"
down_revision = "0005_highlight_spec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "analysis_runs" not in existing:
        return
    columns = {column["name"] for column in inspector.get_columns("analysis_runs")}
    if "cancel_requested_at" not in columns:
        op.add_column(
            "analysis_runs",
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "analysis_runs" not in existing:
        return
    columns = {column["name"] for column in inspector.get_columns("analysis_runs")}
    if "cancel_requested_at" in columns:
        op.drop_column("analysis_runs", "cancel_requested_at")
