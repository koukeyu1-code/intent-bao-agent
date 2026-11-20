from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._tune_sqlite_connection)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )

    @staticmethod
    def _tune_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL + busy_timeout：本地分析期间后台 Worker 与 API 并发读写，
        # 默认的回滚日志模式会让写事务立刻抛 "database is locked"。
        # WAL 允许读写并存；busy_timeout 让写方排队等待（5 秒）而不是失败。
        # wal_autocheckpoint 收紧到 128 页（约 512KB）：WAL 侧车文件保持
        # 很小，手工文件备份只拷主文件时丢失的最近写入窗口随之最小化
        #（完整做法仍是拷贝 *.db 时一并拷贝 -wal/-shm 或用 backup API）。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA wal_autocheckpoint=128")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def create_all(self) -> None:
        # Import every module that declares a table before creating metadata.
        # Authentication lives separately from the business-domain models to
        # keep its password/session handling isolated.
        from . import auth, models  # noqa: F401

        Base.metadata.create_all(self.engine)
        self._upgrade_legacy_schema()

    def _upgrade_legacy_schema(self) -> None:
        """Apply small additive changes needed by pre-Alembic local demo DBs.

        The first MVP was distributed with ``create_all`` startup rather than
        an Alembic upgrade command.  ``create_all`` does not add columns to an
        existing SQLite database, so keep this narrowly scoped, idempotent
        compatibility step until the launcher runs migrations explicitly.
        """

        inspector = inspect(self.engine)
        if "raw_intent_observations" not in inspector.get_table_names():
            return
        columns = {
            column["name"]
            for column in inspector.get_columns("raw_intent_observations")
        }
        if "ai_snapshot" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE raw_intent_observations "
                        "ADD COLUMN ai_snapshot JSON"
                    )
                )
        if "processing_tasks" in inspector.get_table_names():
            task_columns = {
                column["name"]
                for column in inspector.get_columns("processing_tasks")
            }
            if "result_json" not in task_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE processing_tasks "
                            "ADD COLUMN result_json TEXT"
                        )
                    )

    def session(self) -> Session:
        return self.session_factory()


def get_db(request: Request) -> Generator[Session, None, None]:
    db: Database = request.app.state.database
    with db.session() as session:
        yield session
