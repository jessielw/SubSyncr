from __future__ import annotations

import json
from pathlib import Path

from ffsubsyncui.domain import JobStatus, SyncJob, SyncResult, default_output_for
from ffsubsyncui.persistence import QueueStore


def _job(tmp_path: Path) -> SyncJob:
    subtitle = tmp_path / "episode.srt"
    return SyncJob(
        reference_path=tmp_path / "episode.mkv",
        subtitle_path=subtitle,
        output_path=default_output_for(subtitle),
    )


def test_queue_store_restores_unfinished_but_not_successful_jobs(
    tmp_path: Path,
) -> None:
    queued = _job(tmp_path)
    successful = _job(tmp_path)
    successful.status = JobStatus.SUCCEEDED
    successful.result = SyncResult(True, 0)
    store = QueueStore(tmp_path / "state" / "queue.json")
    store.save([queued, successful])
    restored = store.load()
    assert [job.id for job in restored] == [queued.id]


def test_running_job_is_recovered_as_queued(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job.status = JobStatus.RUNNING
    store = QueueStore(tmp_path / "queue.json")
    store.save([job])
    [restored] = store.load()
    assert restored.status is JobStatus.QUEUED
    assert "Interrupted" in restored.last_error


def test_unknown_queue_schema_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"schema_version": 999, "jobs": []}), encoding="utf-8")
    assert QueueStore(path).load() == []
