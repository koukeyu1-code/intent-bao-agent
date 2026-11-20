"""Rename the screen-recording-only stage to match its actual scope.

The stage historically named "画面与PDF解析" only processes materials with
material_type == "录屏" (screen recordings); PDF text extraction lives in
the 文档解析 stage. Rename stored task rows so history, UI and code agree.

Revision ID: 0007_stage_screen_rename
Revises: 0006_analysis_cancel
Create Date: 2026-11-20
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_stage_screen_rename"
down_revision = "0006_analysis_cancel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "processing_tasks" not in existing:
        return
    op.execute(
        "UPDATE processing_tasks SET stage = '录屏解析' WHERE stage = '画面与PDF解析'"
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "processing_tasks" not in existing:
        return
    op.execute(
        "UPDATE processing_tasks SET stage = '画面与PDF解析' WHERE stage = '录屏解析'"
    )
