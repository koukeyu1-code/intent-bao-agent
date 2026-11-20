"""Unify seeded menu-value keys with the import key (casefold).

Seeds originally stored normalized_value via normalize_menu_value (case
preserved, e.g. "27QHD") while imports match via normalize_taxonomy_key
(casefold, e.g. "27qhd"). Re-importing an identical catalogue therefore
created duplicate menu values and platforms (3x3 became 6x6). Seeds now
use the import key; this migration rewrites existing rows to match.

Revision ID: 0008_taxonomy_seed_key
Revises: 0007_stage_screen_rename
Create Date: 2026-11-20
"""

import unicodedata

from alembic import op
import sqlalchemy as sa


revision = "0008_taxonomy_seed_key"
down_revision = "0007_stage_screen_rename"
branch_labels = None
depends_on = None


def _normalize_key(value: str) -> str:
    """Replicate taxonomy_excel.normalize_taxonomy_text + casefold."""

    text = unicodedata.normalize("NFKC", value).replace(" ", " ")
    return " ".join(text.split()).casefold()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "platform_menu_values" not in existing:
        return
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, normalized_value FROM platform_menu_values")
    ).fetchall()
    for row_id, normalized in rows:
        if normalized is None:
            continue
        rewritten = _normalize_key(normalized)
        if rewritten != normalized:
            conn.execute(
                sa.text(
                    "UPDATE platform_menu_values SET normalized_value = :key "
                    "WHERE id = :id"
                ),
                {"key": rewritten, "id": row_id},
            )


def downgrade() -> None:
    # 原值不可从折叠键恢复（大小写信息已丢失）；降级时保留折叠键，
    # 与新种子的行为一致。
    pass
