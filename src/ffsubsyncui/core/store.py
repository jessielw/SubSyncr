"""SQLite persistence for the active queue and completed-job history.

Deliberately stdlib ``sqlite3`` - a single-user desktop queue does not need an
ORM or a migration framework. Schema versioning rides on ``PRAGMA user_version``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from ffsubsyncui.core.models import JobStatus, SyncJob, SyncOptions, SyncResult

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    position        INTEGER NOT NULL,
    enabled         INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    reference_path  TEXT    NOT NULL,
    subtitle_path   TEXT    NOT NULL,
    output_path     TEXT    NOT NULL,
    options         TEXT    NOT NULL,
    language        TEXT,
    group_key       TEXT,
    last_error      TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id                     TEXT PRIMARY KEY,
    finished_at            REAL NOT NULL,
    status                 TEXT NOT NULL,
    reference_path         TEXT NOT NULL,
    subtitle_path          TEXT NOT NULL,
    output_path            TEXT NOT NULL,
    options                TEXT NOT NULL,
    language               TEXT,
    offset_seconds         REAL,
    framerate_scale_factor REAL,
    score                  REAL,
    duration_seconds       REAL,
    used_speech_cache      INTEGER NOT NULL DEFAULT 0,
    error                  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS history_finished_at ON history (finished_at DESC);
"""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: str
    finished_at: float
    status: JobStatus
    reference_path: Path
    subtitle_path: Path
    output_path: Path
    options: SyncOptions
    language: str | None
    offset_seconds: float | None
    framerate_scale_factor: float | None
    score: float | None
    duration_seconds: float | None
    used_speech_cache: bool
    error: str

    def to_job(self) -> SyncJob:
        """Rebuild a queueable job from this record, for retry."""
        return SyncJob(
            reference_path=self.reference_path,
            subtitle_path=self.subtitle_path,
            output_path=self.output_path,
            options=self.options,
            language=self.language,
        )


class JobStore:
    """Thread-safe SQLite store. One instance is shared by the whole app."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(_SCHEMA)
            self._migrate()
            self._connection.commit()

    def _migrate(self) -> None:
        cursor = self._connection.execute("PRAGMA user_version")
        version = int(cursor.fetchone()[0])
        if version >= SCHEMA_VERSION:
            return
        # Future migrations branch on `version` here. Version 1 is the baseline
        # created by _SCHEMA above.
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- active queue ----------------------------------------------------

    def load_jobs(self) -> list[SyncJob]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs ORDER BY position ASC"
            ).fetchall()
        jobs: list[SyncJob] = []
        for row in rows:
            try:
                jobs.append(_job_from_row(row))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue  # Skip a corrupt row rather than lose the whole queue.
        return jobs

    def save_jobs(self, jobs: list[SyncJob]) -> None:
        """Replace the stored queue. Succeeded jobs live in history instead."""
        payload = [
            (
                job.id,
                index,
                int(job.enabled),
                job.status.value,
                str(job.reference_path),
                str(job.subtitle_path),
                str(job.output_path),
                json.dumps(job.options.to_dict(), sort_keys=True),
                job.language,
                job.group_key,
                job.last_error,
                job.created_at,
            )
            for index, job in enumerate(jobs)
            if job.status is not JobStatus.SUCCEEDED
        ]
        with self._lock:
            self._connection.execute("DELETE FROM jobs")
            self._connection.executemany(
                "INSERT INTO jobs (id, position, enabled, status, reference_path,"
                " subtitle_path, output_path, options, language, group_key,"
                " last_error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            self._connection.commit()

    # -- history ---------------------------------------------------------

    def record(self, job: SyncJob, result: SyncResult) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO history (id, finished_at, status,"
                " reference_path, subtitle_path, output_path, options, language,"
                " offset_seconds, framerate_scale_factor, score, duration_seconds,"
                " used_speech_cache, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    time.time(),
                    result.status.value,
                    str(job.reference_path),
                    str(job.subtitle_path),
                    str(job.output_path),
                    json.dumps(job.options.to_dict(), sort_keys=True),
                    job.language,
                    result.offset_seconds,
                    result.framerate_scale_factor,
                    result.score,
                    result.duration_seconds,
                    int(result.used_speech_cache),
                    result.error,
                ),
            )
            self._connection.commit()

    def history(
        self,
        *,
        limit: int = 200,
        search: str = "",
        statuses: tuple[JobStatus, ...] = (),
    ) -> list[HistoryEntry]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if search.strip():
            clauses.append("(subtitle_path LIKE ? OR reference_path LIKE ?)")
            pattern = f"%{search.strip()}%"
            parameters.extend((pattern, pattern))
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(status.value for status in statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM history{where} ORDER BY finished_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_history_from_row(row) for row in rows]

    def prune_history(self, keep: int) -> int:
        """Trim history to the newest ``keep`` rows. Returns rows deleted."""
        if keep <= 0:
            return 0
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM history WHERE id NOT IN ("
                " SELECT id FROM history ORDER BY finished_at DESC LIMIT ?)",
                (keep,),
            )
            self._connection.commit()
            return cursor.rowcount

    def clear_history(self) -> int:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM history")
            self._connection.commit()
            return cursor.rowcount


def _job_from_row(row: sqlite3.Row) -> SyncJob:
    status = JobStatus(str(row["status"]))
    # A RUNNING row means the app died mid-job. Requeue rather than lie.
    interrupted = status is JobStatus.RUNNING
    return SyncJob(
        id=str(row["id"]),
        enabled=bool(row["enabled"]),
        status=JobStatus.QUEUED if interrupted else status,
        reference_path=Path(str(row["reference_path"])),
        subtitle_path=Path(str(row["subtitle_path"])),
        output_path=Path(str(row["output_path"])),
        options=SyncOptions.from_dict(json.loads(str(row["options"]))),
        language=row["language"],
        group_key=row["group_key"],
        last_error=(
            "Interrupted during the previous session"
            if interrupted
            else str(row["last_error"] or "")
        ),
        stage="Interrupted" if interrupted else "Waiting",
        created_at=float(row["created_at"]),
    )


def _history_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=str(row["id"]),
        finished_at=float(row["finished_at"]),
        status=JobStatus(str(row["status"])),
        reference_path=Path(str(row["reference_path"])),
        subtitle_path=Path(str(row["subtitle_path"])),
        output_path=Path(str(row["output_path"])),
        options=SyncOptions.from_dict(json.loads(str(row["options"]))),
        language=row["language"],
        offset_seconds=row["offset_seconds"],
        framerate_scale_factor=row["framerate_scale_factor"],
        score=row["score"],
        duration_seconds=row["duration_seconds"],
        used_speech_cache=bool(row["used_speech_cache"]),
        error=str(row["error"] or ""),
    )
