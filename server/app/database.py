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
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_all(self) -> None:
        from . import models  # noqa: F401

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

    def session(self) -> Session:
        return self.session_factory()


def get_db(request: Request) -> Generator[Session, None, None]:
    db: Database = request.app.state.database
    with db.session() as session:
        yield session
