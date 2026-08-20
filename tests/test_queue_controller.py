from __future__ import annotations

from pathlib import Path

import pytest

from ffsubsyncui.domain import JobStatus, SyncJob, SyncResult
from ffsubsyncui.job_model import JobTableModel
from ffsubsyncui.queue_controller import QueueController
from ffsubsyncui.runner import FFSubsyncRunner, LogCallback, ProgressCallback


def _job(name: str) -> SyncJob:
    return SyncJob(
        Path(f"{name}.mkv"),
        Path(f"{name}.srt"),
        Path(f"{name}.synced.srt"),
    )


def test_queue_continues_after_failure(
    monkeypatch: pytest.MonkeyPatch, qtbot: object
) -> None:
    first = _job("first")
    second = _job("second")
    model = JobTableModel([first, second])
    controller = QueueController(model)

    def fake_run(
        runner: FFSubsyncRunner,
        job: SyncJob,
        *,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
    ) -> SyncResult:
        del runner
        progress_callback(0.5, "Working")
        log_callback(job.reference_path.name)
        if job is first:
            return SyncResult(False, 1, error="first failed")
        return SyncResult(True, 0, offset_seconds=0.25)

    monkeypatch.setattr(FFSubsyncRunner, "run", fake_run)
    controller.start()
    qtbot.waitUntil(  # type: ignore[attr-defined]
        lambda: (
            first.status is JobStatus.FAILED
            and second.status is JobStatus.SUCCEEDED
            and not controller.is_running
        ),
        timeout=5000,
    )
