from __future__ import annotations

import logging
import sqlite3
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from . import auth, models  # noqa: F401  Ensure every table is in Base.metadata.
from .database import Base


logger = logging.getLogger(__name__)


class SchemaMigrationError(RuntimeError):
    """Raised when startup cannot prove that the database schema is safe."""


@dataclass(frozen=True)
class SchemaMigrationResult:
    revision: str
    backup_path: Path | None
    migrated: bool


def _backup_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup, including WAL data."""

    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _restore_sqlite(source: Path, destination: Path) -> None:
    # A failed migration may have left WAL sidecars that no longer belong to
    # the restored main database.  Remove those before replacing the file.
    for suffix in ("-wal", "-shm"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)
    shutil.copy2(source, destination)


def _alembic_config(server_dir: Path, database_url: str) -> Config:
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _sqlite_database_path(database_url: str) -> Path | None:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite" or parsed.path in {"", "/:memory:"}:
        return None
    raw_path = unquote(parsed.path)
    if parsed.netloc:
        raw_path = f"//{parsed.netloc}{raw_path}"
    elif len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        # SQLAlchemy renders an absolute Windows path as /E:/path/file.db.
        raw_path = raw_path[1:]
    return Path(raw_path).resolve()


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        if "alembic_version" not in inspector.get_table_names():
            return None
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    finally:
        engine.dispose()


def _validate_schema(database_url: str, expected_revision: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        missing_tables = set(Base.metadata.tables) - tables
        missing_columns: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in tables:
                continue
            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name in set(table.columns.keys()) - actual_columns:
                missing_columns.append(f"{table_name}.{column_name}")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        if revision != expected_revision or missing_tables or missing_columns:
            details: list[str] = []
            if revision != expected_revision:
                details.append(
                    f"revision={revision!r}, expected={expected_revision!r}"
                )
            if missing_tables:
                details.append("missing tables=" + ", ".join(sorted(missing_tables)))
            if missing_columns:
                details.append("missing columns=" + ", ".join(sorted(missing_columns)))
            raise SchemaMigrationError("数据库结构校验失败：" + "; ".join(details))
    finally:
        engine.dispose()


def migrate_database(server_dir: Path, database_url: str) -> SchemaMigrationResult:
    """Upgrade the application schema before startup, preserving an SQLite copy.

    A non-empty pre-Alembic database is first adopted at revision 0001.  The
    historic 0001 migration uses current metadata, so running it directly over
    an existing database would incorrectly classify that legacy database as a
    fresh installation.  All later idempotent migrations then run normally.
    """

    config = _alembic_config(server_dir, database_url)
    expected_revision = ScriptDirectory.from_config(config).get_current_head()
    sqlite_path = _sqlite_database_path(database_url)
    database_exists = bool(sqlite_path and sqlite_path.is_file())
    backup_path: Path | None = None
    current_revision = _current_revision(database_url) if database_exists else None

    if current_revision == expected_revision:
        _validate_schema(database_url, expected_revision)
        return SchemaMigrationResult(expected_revision, None, False)

    if database_exists and sqlite_path is not None:
        backup_dir = sqlite_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"{sqlite_path.name}.{timestamp}.bak"
        _backup_sqlite(sqlite_path, backup_path)
        logger.warning("数据库升级前备份已创建：%s", backup_path)

    try:
        if database_exists and current_revision is None:
            # Empty files are fresh installations; populated local MVP files
            # predate Alembic and already contain the original domain schema.
            engine = create_engine(database_url, future=True)
            try:
                has_tables = bool(inspect(engine).get_table_names())
            finally:
                engine.dispose()
            if has_tables:
                command.stamp(config, "0001_initial")
        command.upgrade(config, "head")
        _validate_schema(database_url, expected_revision)
    except Exception as exc:
        if backup_path is not None and sqlite_path is not None:
            _restore_sqlite(backup_path, sqlite_path)
            logger.error("数据库升级失败，已从备份恢复：%s", backup_path)
        raise SchemaMigrationError(
            f"数据库升级失败，应用已停止启动：{exc}"
        ) from exc

    return SchemaMigrationResult(expected_revision, backup_path, True)


__all__ = [
    "SchemaMigrationError",
    "SchemaMigrationResult",
    "migrate_database",
]
