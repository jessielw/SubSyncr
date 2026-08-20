from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSaveFile, QStandardPaths

from ffsubsyncui.domain import JobStatus, SyncJob

SCHEMA_VERSION = 1


class QueueStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            data_dir = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppDataLocation
                )
            )
            path = data_dir / "queue.json"
        self.path = path

    def load(self) -> list[SyncJob]:
        if not self.path.exists():
            return []
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != SCHEMA_VERSION
            ):
                return []
            records = payload.get("jobs", [])
            if not isinstance(records, list):
                return []
            jobs: list[SyncJob] = []
            for record in records:
                try:
                    jobs.append(SyncJob.from_record(record))
                except ValueError:
                    continue
            return jobs
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, jobs: list[SyncJob]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "jobs": [
                job.to_record() for job in jobs if job.status is not JobStatus.SUCCEEDED
            ],
        }
        destination = QSaveFile(str(self.path))
        if not destination.open(QSaveFile.OpenModeFlag.WriteOnly):
            raise OSError(destination.errorString())
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        if destination.write(data) != len(data) or not destination.commit():
            raise OSError(destination.errorString())
