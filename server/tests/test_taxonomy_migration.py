from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database import Database


SERVER_ROOT = Path(__file__).resolve().parents[1]
REVISION = "0003_auth_taxonomy"
REVISION_TABLES = {
    "app_users",
    "app_sessions",
    "platform_menu_definitions",
    "platform_menu_values",
    "platform_variants",
    "taxonomy_import_batches",
    "observation_platform_drafts",
    "intent_platform_path_snapshots",
}


def _config(url: str) -> Config:
    result = Config(str(SERVER_ROOT / "alembic.ini"))
    result.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    result.set_main_option("sqlalchemy.url", url)
    return result


@pytest.mark.parametrize("precreate_current_schema", [False, True])
def test_migration_reaches_0003_for_fresh_and_pre_alembic_databases(
    tmp_path: Path, precreate_current_schema: bool
) -> None:
    path = tmp_path / (
        "pre-alembic.db" if precreate_current_schema else "fresh.db"
    )
    url = f"sqlite:///{path.as_posix()}"
    if precreate_current_schema:
        database = Database(url)
        database.create_all()
        database.engine.dispose()

    config = _config(url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    schema = inspect(engine)
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == REVISION
    assert REVISION_TABLES <= set(schema.get_table_names())
    assert "ai_snapshot" in {
        column["name"]
        for column in schema.get_columns("raw_intent_observations")
    }
    engine.dispose()


def test_migrated_schema_matches_current_models(tmp_path: Path) -> None:
    path = tmp_path / "schema-check.db"
    url = f"sqlite:///{path.as_posix()}"
    config = _config(url)
    command.upgrade(config, "head")
    # Raises CommandError if model metadata would require another migration.
    command.check(config)
