"""Add local accounts and the technology-owned platform taxonomy.

Revision ID: 0003_auth_taxonomy
Revises: 0002_ai_snapshot
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_auth_taxonomy"
down_revision = "0002_ai_snapshot"
branch_labels = None
depends_on = None


_REVISION_TABLES = {
    "app_users",
    "app_sessions",
    "platform_menu_definitions",
    "platform_menu_values",
    "platform_variants",
    "taxonomy_import_batches",
    "observation_platform_drafts",
    "intent_platform_path_snapshots",
}


def upgrade() -> None:
    # Local MVP releases originally started with ``Base.metadata.create_all``.
    # Such databases can already contain the complete 0003 structure while
    # having no Alembic version row.  Treat that complete shape as adopted,
    # but reject a partial shape instead of silently leaving an inconsistent DB.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    present = existing & _REVISION_TABLES
    if present:
        missing = _REVISION_TABLES - existing
        if missing:
            raise RuntimeError(
                "0003 schema is partial; missing tables: "
                + ", ".join(sorted(missing))
            )
        return

    op.create_table(
        "app_users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_users_username", "app_users", ["username"], unique=True)
    op.create_index("ix_app_users_role", "app_users", ["role"], unique=False)
    op.create_table(
        "app_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_sessions_user_id", "app_sessions", ["user_id"], unique=False)
    op.create_index("ix_app_sessions_token_hash", "app_sessions", ["token_hash"], unique=True)
    op.create_index("ix_app_sessions_expires_at", "app_sessions", ["expires_at"], unique=False)
    op.create_index("ix_app_sessions_revoked_at", "app_sessions", ["revoked_at"], unique=False)

    op.create_table(
        "platform_menu_definitions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("technology_id", sa.String(length=64), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("taxonomy_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["technology_id"], ["technologies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("technology_id", "level", name="uq_platform_menu_definition"),
    )
    op.create_index("ix_platform_menu_definitions_technology_id", "platform_menu_definitions", ["technology_id"], unique=False)
    op.create_table(
        "platform_menu_values",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("definition_id", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("normalized_value", sa.String(length=200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("activated_by", sa.String(length=100), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taxonomy_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["definition_id"], ["platform_menu_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("definition_id", "normalized_value", name="uq_platform_menu_value"),
    )
    op.create_index("ix_platform_menu_values_definition_id", "platform_menu_values", ["definition_id"], unique=False)
    op.create_index("ix_platform_menu_values_status", "platform_menu_values", ["status"], unique=False)
    op.create_table(
        "platform_variants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("technology_id", sa.String(length=64), nullable=False),
        sa.Column("level2_value_id", sa.String(length=64), nullable=False),
        sa.Column("level3_value_id", sa.String(length=64), nullable=False),
        sa.Column("generic_platform_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("path_snapshot", sa.JSON(), nullable=False),
        sa.Column("taxonomy_revision", sa.Integer(), nullable=False),
        sa.Column("mapping_status", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["generic_platform_id"], ["generic_platforms.id"]),
        sa.ForeignKeyConstraint(["level2_value_id"], ["platform_menu_values.id"]),
        sa.ForeignKeyConstraint(["level3_value_id"], ["platform_menu_values.id"]),
        sa.ForeignKeyConstraint(["technology_id"], ["technologies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("technology_id", "level2_value_id", "level3_value_id", name="uq_platform_variant_path"),
        sa.UniqueConstraint("generic_platform_id"),
    )
    op.create_index("ix_platform_variants_technology_id", "platform_variants", ["technology_id"], unique=False)
    op.create_index("ix_platform_variants_level2_value_id", "platform_variants", ["level2_value_id"], unique=False)
    op.create_index("ix_platform_variants_level3_value_id", "platform_variants", ["level3_value_id"], unique=False)
    op.create_index("ix_platform_variants_generic_platform_id", "platform_variants", ["generic_platform_id"], unique=True)
    op.create_table(
        "taxonomy_import_batches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("committed_by", sa.String(length=100), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_taxonomy_import_batches_file_sha256", "taxonomy_import_batches", ["file_sha256"], unique=True)
    op.create_table(
        "observation_platform_drafts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("technology_id", sa.String(length=64), nullable=False),
        sa.Column("level2_value_id", sa.String(length=64), nullable=True),
        sa.Column("level3_value_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposed_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["level2_value_id"], ["platform_menu_values.id"]),
        sa.ForeignKeyConstraint(["level3_value_id"], ["platform_menu_values.id"]),
        sa.ForeignKeyConstraint(["observation_id"], ["raw_intent_observations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["technology_id"], ["technologies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id"),
    )
    op.create_index("ix_observation_platform_drafts_observation_id", "observation_platform_drafts", ["observation_id"], unique=True)
    op.create_index("ix_observation_platform_drafts_technology_id", "observation_platform_drafts", ["technology_id"], unique=False)
    op.create_table(
        "intent_platform_path_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("technology_id", sa.String(length=64), nullable=True),
        sa.Column("generic_platform_id", sa.String(length=64), nullable=True),
        sa.Column("platform_variant_id", sa.String(length=64), nullable=True),
        sa.Column("taxonomy_revision", sa.Integer(), nullable=True),
        sa.Column("mapping_status", sa.String(length=32), nullable=False),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.Column("captured_by", sa.String(length=100), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_type", "target_id", "version_no", name="uq_intent_platform_path_snapshot"),
    )
    op.create_index("ix_intent_platform_path_snapshots_target_type", "intent_platform_path_snapshots", ["target_type"], unique=False)
    op.create_index("ix_intent_platform_path_snapshots_target_id", "intent_platform_path_snapshots", ["target_id"], unique=False)


def downgrade() -> None:
    for table in (
        "intent_platform_path_snapshots",
        "observation_platform_drafts",
        "taxonomy_import_batches",
        "platform_variants",
        "platform_menu_values",
        "platform_menu_definitions",
        "app_sessions",
        "app_users",
    ):
        op.drop_table(table)
