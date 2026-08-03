"""Media pipeline tables and stage result persistence.

Revision ID: 0004_media_pipeline
Revises: 0003_auth_taxonomy
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_media_pipeline"
down_revision = "0003_auth_taxonomy"
branch_labels = None
depends_on = None


_NEW_TABLES = {
    "transcript_segments",
    "pdf_page_texts",
    "key_frames",
    "alignment_links",
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "processing_tasks" in existing:
        columns = {
            column["name"]
            for column in inspector.get_columns("processing_tasks")
        }
        if "result_json" not in columns:
            op.add_column(
                "processing_tasks",
                sa.Column("result_json", sa.Text(), nullable=True),
            )

    present = existing & _NEW_TABLES
    if present:
        missing = _NEW_TABLES - existing
        if missing:
            raise RuntimeError(
                "0004 schema is partial; missing tables: "
                + ", ".join(sorted(missing))
            )
        return

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=64), nullable=False),
        sa.Column("material_file_id", sa.String(length=64), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("speaker_label", sa.String(length=64), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transcript_segments_analysis_run_id",
        "transcript_segments",
        ["analysis_run_id"],
    )
    op.create_index(
        "ix_transcript_segments_package_id",
        "transcript_segments",
        ["package_id"],
    )
    op.create_index(
        "ix_transcript_segments_material_file_id",
        "transcript_segments",
        ["material_file_id"],
    )

    op.create_table(
        "pdf_page_texts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("material_file_id", sa.String(length=64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("extracted_by", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pdf_page_texts_material_file_id",
        "pdf_page_texts",
        ["material_file_id"],
    )

    op.create_table(
        "key_frames",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("material_file_id", sa.String(length=64), nullable=False),
        sa.Column("time_ms", sa.Integer(), nullable=True),
        sa.Column("image_relative_path", sa.String(length=500), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("page_candidates_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_key_frames_material_file_id",
        "key_frames",
        ["material_file_id"],
    )

    op.create_table(
        "alignment_links",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("segment_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alignment_links_analysis_run_id",
        "alignment_links",
        ["analysis_run_id"],
    )
    op.create_index(
        "ix_alignment_links_package_id",
        "alignment_links",
        ["package_id"],
    )
    op.create_index(
        "ix_alignment_links_segment_id",
        "alignment_links",
        ["segment_id"],
    )


def downgrade() -> None:
    op.drop_table("alignment_links")
    op.drop_table("key_frames")
    op.drop_table("pdf_page_texts")
    op.drop_table("transcript_segments")
    op.drop_column("processing_tasks", "result_json")
