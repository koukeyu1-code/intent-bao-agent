from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings kept outside business data."""

    server_dir: Path
    data_dir: Path
    database_url: str
    model_adapter: str = "mock"

    @classmethod
    def load(
        cls,
        *,
        database_url: str | None = None,
        data_dir: str | Path | None = None,
    ) -> "Settings":
        server_dir = Path(__file__).resolve().parents[1]
        resolved_data_dir = Path(
            data_dir or os.getenv("INTENT_BAO_DATA_DIR", server_dir / "data")
        ).resolve()
        resolved_data_dir.mkdir(parents=True, exist_ok=True)
        for child in ("database", "projects", "logs", "temp"):
            (resolved_data_dir / child).mkdir(parents=True, exist_ok=True)

        resolved_database_url = database_url or os.getenv(
            "INTENT_BAO_DATABASE_URL",
            f"sqlite:///{(resolved_data_dir / 'database' / 'intent_bao.db').as_posix()}",
        )
        return cls(
            server_dir=server_dir,
            data_dir=resolved_data_dir,
            database_url=resolved_database_url,
            model_adapter=os.getenv("INTENT_BAO_MODEL_ADAPTER", "mock").strip().lower(),
        )

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"
