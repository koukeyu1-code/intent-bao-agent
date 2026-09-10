"""Private, resumable ASR results; these files are never published as evidence."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .audio_adapters import AsrSegment


class AsrCheckpoints:
    def __init__(
        self, root: Path, source: Path, *, config: dict[str, Any],
        run_id: str, check_cancelled: Callable[[], None],
    ) -> None:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                check_cancelled()
                digest.update(chunk)
        self.fingerprint = hashlib.sha256(json.dumps(
            {"version": 1, "source_sha256": digest.hexdigest(), "config": config},
            sort_keys=True, ensure_ascii=False, default=str,
        ).encode()).hexdigest()
        self.directory = root / self.fingerprint
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

    def _path(self, job: tuple[int, int, str | None]) -> Path:
        key = hashlib.sha256(json.dumps(job).encode()).hexdigest()
        return self.directory / f"{key}.json"

    def load(self, job: tuple[int, int, str | None]) -> list[AsrSegment] | None:
        try:
            value = json.loads(self._path(job).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            if value.get("fingerprint") != self.fingerprint or value.get("job") != list(job):
                return None
            raw_rows = value.get("segments")
            if not isinstance(raw_rows, list) or not raw_rows:
                return None
            rows = []
            for row in raw_rows:
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    return None
                if not row["text"].strip():
                    return None
                if any(type(row.get(key)) is not int for key in ("start_ms", "end_ms")):
                    return None
                if row["start_ms"] < job[0] or row["end_ms"] > job[1] or row["end_ms"] <= row["start_ms"]:
                    return None
                if any(row.get(key) is not None and not isinstance(row[key], str)
                       for key in ("speaker_label", "language")):
                    return None
                confidence = row.get("confidence")
                if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
                    return None
                rows.append(AsrSegment(**row))
            return rows
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, job: tuple[int, int, str | None], rows: list[AsrSegment]) -> None:
        destination = self._path(job)
        temporary = destination.with_suffix(f".{self.run_id}.tmp")
        payload = {
            "fingerprint": self.fingerprint, "job": list(job),
            "source_run_id": self.run_id, "segments": [asdict(row) for row in rows],
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
