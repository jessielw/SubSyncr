"""Queue behaviour, with the subprocess stubbed out.

Real ffsubsync runs are covered by tests/test_integration.py; these are about
state transitions, persistence and the speech-cache handshake.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any, ClassVar

import numpy as np
import pytest

from subsyncr.core import queue as queue_module
from subsyncr.core.config import AppConfig
from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, SyncJob, SyncResult
from subsyncr.core.queue import QueueCallbacks, SyncQueue
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore


class FakeProcess:
    """Stands in for SyncProcess, recording what the queue asked for.

    One instance per *group*, matching the real engine, so tests can assert how
    many processes a batch actually costs.
    """

    instances: ClassVar[list[FakeProcess]] = []
    result_status = JobStatus.SUCCEEDED
    delay = 0.0

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.items = list(spec["items"])
        self.cancelled = False
        self._started = threading.Event()
        FakeProcess.instances.append(self)

    def cancel(self) -> None:
        self.cancelled = True

    def run(
        self, *, on_progress: Any = None, on_log: Any = None
    ) -> dict[str, SyncResult]:
        self._started.set()
        if self.spec["serialize_speech"]:
            # The worker writes only its assigned cache staging file.
            np.savez(self.spec["speech_output"], speech=[0, 1, 0])
        if on_progress is not None:
            # job_id None means the group-wide extraction.
            on_progress(None, 0.5, "Extracting speech")
        if on_log is not None:
            on_log(None, "INFO", "working")
        if FakeProcess.delay:
            time.sleep(FakeProcess.delay)
        results: dict[str, SyncResult] = {}
        for index, item in enumerate(self.items):
            if self.cancelled:
                results[item["id"]] = SyncResult(
                    status=JobStatus.CANCELLED, error="Cancelled"
                )
                continue
            results[item["id"]] = SyncResult(
                status=FakeProcess.result_status,
                offset_seconds=-1.5,
                error=(
                    "" if FakeProcess.result_status is JobStatus.SUCCEEDED else "boom"
                ),
                shared_extraction=index > 0,
            )
        return results


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


def test_a_crash_mid_run_fails_the_group_rather_than_hanging(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception must not leave the queue looking busy forever."""

    def explode(self: FakeProcess, **kwargs: Any) -> dict[str, SyncResult]:
        raise RuntimeError("bug in the queue")

    monkeypatch.setattr(FakeProcess, "run", explode)
    crashes: list[BaseException | None] = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: crashes.append(args.exc_value)
    )

    queue = build_queue()
    queue.add([make_job("A")])
    _drain(queue)

    job = queue.snapshot()[0]
    assert job.status is JobStatus.FAILED
    assert "bug in the queue" in job.last_error
    # ...and the bug still reaches the hook that reports it, rather than being
    # quietly absorbed into a failed job.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not crashes:
        time.sleep(0.01)
    assert crashes and isinstance(crashes[0], RuntimeError)


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
    # Stop first: a still-draining queue claims the requeued job immediately,
    # so QUEUED would be a race rather than the state under test.
    queue.stop()
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

    # One process for the whole group, so the extraction happens exactly once.
    assert len(FakeProcess.instances) == 1
    assert len(FakeProcess.instances[0].items) == 3

    results = [job.result for job in queue.snapshot()]
    assert sum(1 for r in results if r and r.shared_extraction) == 2

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


def test_a_solo_job_still_populates_the_cache(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    """So a second language for the same video is instant tomorrow."""
    queue = build_queue()
    queue.add([make_job("Solo", group_key=None)])
    _drain(queue)
    assert FakeProcess.instances[0].spec["serialize_speech"] is True


def test_subtitle_references_are_never_cached(
    build_queue: Callable[..., SyncQueue], tmp_path: Any
) -> None:
    """Nothing expensive was extracted, so there is nothing worth storing."""
    reference = tmp_path / "reference.srt"
    reference.touch()
    subtitle = tmp_path / "input.srt"
    subtitle.touch()
    queue = build_queue()
    queue.add(
        [
            SyncJob(
                reference_path=reference,
                subtitle_path=subtitle,
                output_path=tmp_path / "input.synced.srt",
                group_key="subs",
            )
        ]
    )
    _drain(queue)
    assert FakeProcess.instances[0].spec["serialize_speech"] is False


def test_each_video_gets_its_own_process(
    build_queue: Callable[..., SyncQueue], tmp_path: Any
) -> None:
    """Different references cannot share an extraction."""
    jobs = []
    for index in range(3):
        reference = tmp_path / f"Show.S01E0{index}.mkv"
        reference.touch()
        subtitle = tmp_path / f"Show.S01E0{index}.en.srt"
        subtitle.touch()
        jobs.append(
            SyncJob(
                reference_path=reference,
                subtitle_path=subtitle,
                output_path=tmp_path / f"Show.S01E0{index}.en.synced.srt",
                group_key=f"group-{index}",
            )
        )
    queue = build_queue()
    queue.add(jobs)
    assert queue.pending_groups() == 3
    _drain(queue)
    assert len(FakeProcess.instances) == 3


def test_different_engines_on_one_reference_run_separately(
    build_queue: Callable[..., SyncQueue], make_job: Callable[..., SyncJob]
) -> None:
    """A per-batch engine pick must not be folded into another batch's group."""
    first = make_job("Show.S01E01", group_key="episode")
    second = make_job("Show.S01E01", group_key="episode")
    second.options = second.options.with_engine(EngineId.ALASS)
    queue = build_queue()
    queue.add([first, second])
    assert queue.pending_groups() == 2


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


def test_cancelling_one_job_stops_its_whole_group(
    build_queue: Callable[..., SyncQueue], tmp_path: Any
) -> None:
    """Siblings share one ffsubsync run and cannot be separated.

    The alternative would be to pretend a group member can be stopped alone,
    which would silently leave the others running.
    """
    FakeProcess.delay = 1.0
    reference = tmp_path / "Show.mkv"
    reference.touch()
    jobs = []
    for language in ("en", "es"):
        subtitle = tmp_path / f"Show.{language}.srt"
        subtitle.touch()
        jobs.append(
            SyncJob(
                reference_path=reference,
                subtitle_path=subtitle,
                output_path=tmp_path / f"Show.{language}.synced.srt",
                group_key="shared",
            )
        )
    queue = build_queue()
    queue.add(jobs)
    queue.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not FakeProcess.instances:
        time.sleep(0.01)
    assert queue.cancel(jobs[0].id) is True

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and queue.counts().running:
        time.sleep(0.02)
    queue.shutdown()

    assert all(job.status is JobStatus.CANCELLED for job in queue.snapshot())
    # One process was cancelled, not two.
    assert len(FakeProcess.instances) == 1


def test_cancelling_an_unknown_job_is_harmless(
    build_queue: Callable[..., SyncQueue],
) -> None:
    assert build_queue().cancel("no such job") is False


def test_cancel_returns_without_waiting_for_the_worker(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel is pressed on the GUI thread; a slow stop must not freeze it.

    A real worker can take seconds to stop (grace period, terminate, kill).
    """
    released = threading.Event()

    def slow_cancel(self: FakeProcess) -> None:
        released.wait(5)
        self.cancelled = True

    monkeypatch.setattr(FakeProcess, "cancel", slow_cancel)
    FakeProcess.delay = 1.0
    queue = build_queue()
    queue.add([make_job("A")])
    queue.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not queue._processes:
        time.sleep(0.01)

    started = time.monotonic()
    assert queue.cancel_all() == 1
    assert time.monotonic() - started < 0.5
    released.set()
    queue.shutdown(timeout=8.0)
    # shutdown still waited for the stop to land.
    assert FakeProcess.instances[0].cancelled is True


# -- engine fallback chain -----------------------------------------------


class ScriptedProcess(FakeProcess):
    """A stub whose outcome depends on which engine the queue asked for."""

    # engine id -> status it returns.
    script: ClassVar[dict[str, JobStatus]] = {}
    engines_used: ClassVar[list[str]] = []

    def run(
        self, *, on_progress: Any = None, on_log: Any = None
    ) -> dict[str, SyncResult]:
        engine = str(self.spec["engine"])
        ScriptedProcess.engines_used.append(engine)
        status = ScriptedProcess.script.get(engine, JobStatus.FAILED)
        return {
            item["id"]: SyncResult(
                status=status,
                engine=EngineId(engine),
                error="" if status is JobStatus.SUCCEEDED else "no good",
            )
            for item in self.items
        }


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> type[ScriptedProcess]:
    ScriptedProcess.instances = []
    ScriptedProcess.engines_used = []
    ScriptedProcess.script = {}
    monkeypatch.setattr(queue_module, "SyncProcess", ScriptedProcess)
    return ScriptedProcess


def _chained(job: SyncJob, *chain: EngineId) -> SyncJob:
    job.options.fallback_chain = chain
    return job


def test_a_failure_moves_to_the_next_engine(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
) -> None:
    scripted.script = {"ffsubsync": JobStatus.FAILED, "alass": JobStatus.SUCCEEDED}
    queue = build_queue()
    job = _chained(make_job("A"), EngineId.ALASS)
    queue.add([job])
    _drain(queue)
    queue.shutdown()

    assert scripted.engines_used == ["ffsubsync", "alass"]
    assert job.status is JobStatus.SUCCEEDED
    # History has to say which engine actually produced the file.
    assert job.result is not None
    assert job.result.engine is EngineId.ALASS


def test_a_success_never_reaches_the_fallback(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
) -> None:
    scripted.script = {"ffsubsync": JobStatus.SUCCEEDED, "alass": JobStatus.SUCCEEDED}
    queue = build_queue()
    queue.add([_chained(make_job("A"), EngineId.ALASS)])
    _drain(queue)
    queue.shutdown()
    assert scripted.engines_used == ["ffsubsync"]


def test_a_low_quality_verdict_is_worth_another_engine(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
) -> None:
    """An engine refusing to trust its own answer is exactly when to try another."""
    scripted.script = {
        "ffsubsync": JobStatus.SKIPPED_LOW_QUALITY,
        "lapse": JobStatus.SUCCEEDED,
    }
    queue = build_queue()
    queue.add([_chained(make_job("A"), EngineId.LAPSE)])
    _drain(queue)
    queue.shutdown()
    assert scripted.engines_used == ["ffsubsync", "lapse"]


def test_the_chain_stops_when_every_engine_has_had_a_go(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
) -> None:
    scripted.script = {}  # Everything fails.
    queue = build_queue()
    job = _chained(make_job("A"), EngineId.ALASS, EngineId.LAPSE)
    queue.add([job])
    _drain(queue)
    queue.shutdown()
    assert scripted.engines_used == ["ffsubsync", "alass", "lapse"]
    assert job.status is JobStatus.FAILED


def test_an_engine_that_cannot_read_the_inputs_is_skipped(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No point spending a process on autosubsync for a subtitle reference."""
    scripted.script = {"lapse": JobStatus.SUCCEEDED}
    monkeypatch.setattr(
        queue_module,
        "can_run",
        lambda job, engine: engine is not EngineId.AUTOSUBSYNC,
    )
    queue = build_queue()
    queue.add([_chained(make_job("A"), EngineId.AUTOSUBSYNC, EngineId.LAPSE)])
    _drain(queue)
    queue.shutdown()
    assert "autosubsync" not in scripted.engines_used
    assert scripted.engines_used == ["ffsubsync", "lapse"]


def test_no_chain_means_a_failure_is_final(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
) -> None:
    scripted.script = {}
    queue = build_queue()
    queue.add([make_job("A")])
    _drain(queue)
    queue.shutdown()
    assert scripted.engines_used == ["ffsubsync"]


def test_skipping_an_unable_engine_says_why(
    build_queue: Callable[..., SyncQueue],
    make_job: Callable[..., SyncJob],
    scripted: type[ScriptedProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picked engine never ran, so "did not settle it" would be wrong."""
    scripted.script = {"alass": JobStatus.SUCCEEDED}
    monkeypatch.setattr(
        queue_module, "can_run", lambda job, engine: engine is not EngineId.FFSUBSYNC
    )
    logs: list[tuple[str, str, str]] = []
    queue = build_queue(callbacks=QueueCallbacks(log=lambda *a: logs.append(a)))
    queue.add([_chained(make_job("A"), EngineId.ALASS)])
    _drain(queue)
    queue.shutdown()
    assert scripted.engines_used == ["alass"]
    assert any("ffsubsync cannot handle these files" in log[2] for log in logs)


@pytest.mark.parametrize("replace_output", [False, True])
def test_colliding_group_outputs_are_rejected(build_queue, make_job, replace_output):
    queue = build_queue()
    first = make_job("A", group_key="group")
    second = make_job("B", group_key="group")
    second.reference_path = first.reference_path
    second.output_path = first.output_path
    first.options.files.replace_output = replace_output
    second.options.files.replace_output = replace_output
    queue.add([first, second])
    _drain(queue)
    assert all(job.status is JobStatus.FAILED for job in (first, second))
    assert all("conflicts" in job.last_error for job in (first, second))
    assert not FakeProcess.instances


def test_output_cannot_replace_another_queued_input(build_queue, make_job):
    queue = build_queue()
    first, second = make_job("A"), make_job("B")
    first.output_path = second.subtitle_path
    first.options.files.replace_output = True
    queue.add([first, second])
    _drain(queue)
    assert first.status is JobStatus.FAILED
    assert "conflicts" in first.last_error


def test_cache_does_not_touch_existing_media_sidecar(build_queue, make_job):
    queue = build_queue()
    job = make_job("Film")
    sidecar = job.reference_path.with_suffix(".npz")
    sidecar.write_bytes(b"unrelated user data")
    queue.add([job])
    _drain(queue)
    assert job.status is JobStatus.SUCCEEDED
    assert sidecar.read_bytes() == b"unrelated user data"
