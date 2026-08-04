"""Initial 意向宝 domain schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-30
"""

from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# ``0001`` historically used live SQLAlchemy metadata rather than a frozen
# schema snapshot.  Keep later tables out of the initial revision when the
# migration chain is run with today's models; revision 0003 owns them.
_POST_0001_TABLES = {
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
    # Base metadata is the single initial source; later changes use explicit deltas.
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in _POST_0001_TABLES
    ]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
