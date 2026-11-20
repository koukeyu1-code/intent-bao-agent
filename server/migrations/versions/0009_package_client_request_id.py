"""Add client_request_id to meeting packages for idempotent create retries.

Revision ID: 0009_package_client_request_id
Revises: 0008_taxonomy_seed_key
Create Date: 2026-11-20
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_package_client_request_id"
down_revision = "0008_taxonomy_seed_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "meeting_packages" not in existing:
        return
    columns = {column["name"] for column in inspector.get_columns("meeting_packages")}
    if "client_request_id" not in columns:
        op.add_column(
            "meeting_packages",
            sa.Column("client_request_id", sa.String(64), nullable=True),
        )
        op.create_index(
            "ix_meeting_packages_client_request_id",
            "meeting_packages",
            ["client_request_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "meeting_packages" not in existing:
        return
    columns = {column["name"] for column in inspector.get_columns("meeting_packages")}
    if "client_request_id" in columns:
        op.drop_index(
            "ix_meeting_packages_client_request_id", table_name="meeting_packages"
        )
        op.drop_column("meeting_packages", "client_request_id")
