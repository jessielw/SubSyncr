from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from ffsubsyncui.domain import JobStatus, SyncJob, SyncResult
from ffsubsyncui.job_model import JobTableModel


def _job(name: str) -> SyncJob:
    return SyncJob(Path(f"{name}.mkv"), Path(f"{name}.srt"), Path(f"{name}.synced.srt"))


def test_enabled_checkbox_controls_runnable_jobs(qtbot: object) -> None:
    del qtbot
    job = _job("one")
    model = JobTableModel([job])
    index = model.index(0, 0)
    assert model.setData(
        index, Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole
    )
    assert model.next_runnable() is None
    assert not job.enabled


def test_failure_can_be_retried(qtbot: object) -> None:
    del qtbot
    job = _job("one")
    model = JobTableModel([job])
    model.finish(job.id, SyncResult(False, 1, error="broken"))
    assert job.status is JobStatus.FAILED
    model.retry_rows([0])
    assert job.status is JobStatus.QUEUED
    assert job.last_error == ""
