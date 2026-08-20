"""Queue behaviour, with the subprocess stubbed out.

Real ffsubsync runs are covered by tests/test_integration.py; these are about
state transitions, persistence and the speech-cache handshake.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
import time
from typing import Any, ClassVar

import pytest

from ffsubsyncui.core import queue as queue_module
from ffsubsyncui.core.config import AppConfig
from ffsubsyncui.core.models import JobStatus, SyncJob, SyncResult
from ffsubsyncui.core.queue import QueueCallbacks, SyncQueue
from ffsubsyncui.core.speech_cache import SpeechCache
from ffsubsyncui.core.store import JobStore


class FakeProcess:
    """Stands in for SyncProcess, recording what the queue asked for."""

    instances: ClassVar[list[FakeProcess]] = []
    result_status = JobStatus.SUCCEEDED
    delay = 0.0

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.cancelled = False
        self._started = threading.Event()
        FakeProcess.instances.append(self)

    def cancel(self) -> None:
        self.cancelled = True

    def run(self, *, on_progress: Any = None, on_log: Any = None) -> SyncResult:
        self._started.set()
        if self.spec["serialize_speech"]:
            # ffsubsync writes the speech array beside the reference file.
            Path(self.spec["reference"]).with_suffix(".npz").write_bytes(b"speech")
        if on_progress is not None:
            on_progress(0.5, "Extracting speech")
        if on_log is not None:
            on_log("INFO", "working")
        if FakeProcess.delay:
            time.sleep(FakeProcess.delay)
        if self.cancelled:
            return SyncResult(status=JobStatus.CANCELLED, error="Cancelled")
        return SyncResult(
            status=FakeProcess.result_status,
            offset_seconds=-1.5,
            error="" if FakeProcess.result_status is JobStatus.SUCCEEDED else "boom",
        )


@pytest.fixture(autouse=True)
def _stub_process(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeProcess.instances = []
    FakeProcess.result_status = JobStatus.SUCCEEDED
    FakeProcess.delay = 0.0
    monkeypatch.setattr(queue_module, "SyncProcess", FakeProcess)
    # validate() touches the filesystem and ffmpeg; not what these tests are for.
    monkeypatch.setattr(queue_module, "validate", lambda job: None)


@pytest.fixture
def build_queue(
    store: JobStore, config: AppConfig, tmp_path: Any
) -> Callable[..., SyncQueue]:
    def build(**kwargs: Any) -> SyncQueue:
        cache = SpeechCache(tmp_path / "speech", enabled=True)
        return SyncQueue(store, config, cache, **kwargs)

    return build


def _drain(queue: SyncQueue, timeout: float = 10.0) -> None:
    queue.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        counts = queue.counts()
        if counts.ready == 0 and counts.running == 0:
            return
        time.sleep(0.01)
    raise AssertionError("queue did not drain")


def test_jobs_run_and_report_success(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    queue = build_queue()
    queue.add([make_job("A"), make_job("B")])
    _drain(queue)
    assert queue.counts().succeeded == 2
    assert all(job.result is not None for job in queue.snapshot())


def test_failure_is_recorded_on_the_job(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    FakeProcess.result_status = JobStatus.FAILED
    queue = build_queue()
    queue.add([make_job("A")])
    _drain(queue)
    job = queue.snapshot()[0]
    assert job.status is JobStatus.FAILED
    assert job.last_error == "boom"


def test_disabled_jobs_are_left_alone(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    queue = build_queue()
    first, second = make_job("A"), make_job("B")
    second.enabled = False
    queue.add([first, second])
    _drain(queue)
    assert queue.find(second.id) is not None
    assert queue.find(second.id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_retry_requeues_only_retryable_jobs(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    FakeProcess.result_status = JobStatus.FAILED
    queue = build_queue()
    job = make_job("A")
    queue.add([job])
    _drain(queue)
    assert queue.retry([job.id]) == 1
    assert queue.find(job.id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    # A succeeded job is not retryable through this path.
    FakeProcess.result_status = JobStatus.SUCCEEDED
    _drain(queue)
    assert queue.retry([job.id]) == 0


def test_running_jobs_cannot_be_removed(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    FakeProcess.delay = 0.4
    queue = build_queue()
    job = make_job("A")
    queue.add([job])
    queue.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and queue.counts().running == 0:
        time.sleep(0.01)
    assert queue.remove([job.id]) == 0
    queue.shutdown()


def test_clear_finished_keeps_pending_work(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    queue = build_queue()
    done, pending = make_job("A"), make_job("B")
    pending.enabled = False
    queue.add([done, pending])
    _drain(queue)
    assert queue.clear_finished() == 1
    assert [job.id for job in queue.snapshot()] == [pending.id]


def test_callbacks_fire_for_progress_and_logs(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    progress: list[tuple[str, float | None, str]] = []
    logs: list[tuple[str, str, str]] = []
    queue = build_queue(
        callbacks=QueueCallbacks(
            progress=lambda *a: progress.append(a), log=lambda *a: logs.append(a)
        )
    )
    queue.add([make_job("A")])
    _drain(queue)
    assert progress and progress[0][1] == 0.5
    assert logs and logs[0][2] == "working"


def test_history_is_recorded_for_every_finished_job(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    store: JobStore,
) -> None:
    queue = build_queue()
    queue.add([make_job("A"), make_job("B")])
    _drain(queue)
    assert len(store.history(limit=10)) == 2


def test_succeeded_jobs_are_not_persisted_as_pending_work(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    store: JobStore,
) -> None:
    queue = build_queue()
    queue.add([make_job("A")])
    _drain(queue)
    assert store.load_jobs() == []


def test_persist_queue_false_leaves_the_stored_queue_alone(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    store: JobStore,
) -> None:
    """The CLI must not wipe the queue the GUI saved."""
    saved = make_job("GUI")
    store.save_jobs([saved])
    queue = build_queue(persist_queue=False)
    queue.add([make_job("CLI")])
    _drain(queue)
    assert [job.id for job in store.load_jobs()] == [saved.id]


def test_only_the_first_job_in_a_group_extracts_speech(
    build_queue: Callable[..., SyncQueue], tmp_path: Any
) -> None:
    """One video, three languages: extract audio once, not three times."""
    reference = tmp_path / "Show.S01E01.mkv"
    reference.touch()
    jobs = []
    for language in ("en", "es", "fr"):
        subtitle = tmp_path / f"Show.S01E01.{language}.srt"
        subtitle.touch()
        jobs.append(
            SyncJob(
                reference_path=reference,
                subtitle_path=subtitle,
                output_path=tmp_path / f"Show.S01E01.{language}.synced.srt",
                language=language,
                group_key="shared",
            )
        )
    queue = build_queue()
    queue.add(jobs)
    _drain(queue)

    serializing = [p for p in FakeProcess.instances if p.spec["serialize_speech"]]
    assert len(serializing) == 1, "audio should be extracted exactly once"

    reused = [p for p in FakeProcess.instances if p.spec["reference"].endswith(".npz")]
    assert len(reused) == 2, "the other two should read the cached speech array"

    # The harvested array must not be left lying beside the user's media.
    assert not list(tmp_path.glob("*.npz"))


def test_speech_cache_survives_for_a_later_batch(
    build_queue: Callable[..., SyncQueue], tmp_path: Any
) -> None:
    """A second session against the same video should not re-extract."""
    reference = tmp_path / "Movie.mkv"
    reference.touch()

    def job_for(language: str) -> SyncJob:
        subtitle = tmp_path / f"Movie.{language}.srt"
        subtitle.touch()
        return SyncJob(
            reference_path=reference,
            subtitle_path=subtitle,
            output_path=tmp_path / f"Movie.{language}.synced.srt",
            group_key="movie",
        )

    first = build_queue()
    first.add([job_for("en")])
    _drain(first)
    assert FakeProcess.instances[0].spec["serialize_speech"] is True

    FakeProcess.instances = []
    second = build_queue()
    second.add([job_for("es")])
    _drain(second)
    assert FakeProcess.instances[0].spec["serialize_speech"] is False
    assert FakeProcess.instances[0].spec["reference"].endswith(".npz")


def test_jobs_without_a_group_never_serialize(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    queue = build_queue()
    queue.add([make_job("Solo", group_key=None)])
    _drain(queue)
    assert not any(p.spec["serialize_speech"] for p in FakeProcess.instances)


def test_shutdown_cancels_running_work(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    FakeProcess.delay = 5.0
    queue = build_queue()
    queue.add([make_job("A")])
    queue.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not FakeProcess.instances:
        time.sleep(0.01)
    queue.shutdown(timeout=8.0)
    assert FakeProcess.instances[0].cancelled is True
