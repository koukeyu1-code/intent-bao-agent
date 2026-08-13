from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database import Database
from app.schema_migrations import SchemaMigrationError, migrate_database


SERVER_ROOT = Path(__file__).resolve().parents[1]
REVISION = "0006_analysis_cancel"
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
MEDIA_TABLES = {
    "transcript_segments",
    "pdf_page_texts",
    "key_frames",
    "alignment_links",
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
    assert MEDIA_TABLES <= set(schema.get_table_names())
    assert "ai_snapshot" in {
        column["name"]
        for column in schema.get_columns("raw_intent_observations")
    }
    assert "highlight_spec" in {
        column["name"]
        for column in schema.get_columns("raw_intent_observations")
    }
    assert "cancel_requested_at" in {
        column["name"] for column in schema.get_columns("analysis_runs")
    }
    engine.dispose()


def test_migrated_schema_matches_current_models(tmp_path: Path) -> None:
    path = tmp_path / "schema-check.db"
    url = f"sqlite:///{path.as_posix()}"
    config = _config(url)
    command.upgrade(config, "head")
    # Raises CommandError if model metadata would require another migration.
    command.check(config)


def test_startup_migration_backs_up_and_upgrades_pre_alembic_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    url = f"sqlite:///{path.as_posix()}"
    database = Database(url)
    database.create_all()
    with database.engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE raw_intent_observations DROP COLUMN highlight_spec")
        )
        connection.execute(
            text("ALTER TABLE analysis_runs DROP COLUMN cancel_requested_at")
        )
    database.engine.dispose()

    result = migrate_database(SERVER_ROOT, url)

    assert result.revision == REVISION
    assert result.migrated is True
    assert result.backup_path is not None and result.backup_path.is_file()
    schema = inspect(create_engine(url))
    assert "highlight_spec" in {
        column["name"]
        for column in schema.get_columns("raw_intent_observations")
    }
    assert "cancel_requested_at" in {
        column["name"] for column in schema.get_columns("analysis_runs")
    }


def test_startup_migration_failure_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "restore.db"
    url = f"sqlite:///{path.as_posix()}"
    database = Database(url)
    database.create_all()
    database.engine.dispose()

    def fail_after_mutation(_config: Config, _revision: str) -> None:
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE customers"))
        engine.dispose()
        raise RuntimeError("forced migration failure")

    monkeypatch.setattr("app.schema_migrations.command.upgrade", fail_after_mutation)
    with pytest.raises(SchemaMigrationError, match="应用已停止启动"):
        migrate_database(SERVER_ROOT, url)

    engine = create_engine(url)
    assert "customers" in inspect(engine).get_table_names()
    engine.dispose()
