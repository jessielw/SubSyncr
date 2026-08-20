"""The job queue: a thread pool of cancellable ffsubsync subprocesses.

Callbacks fire on worker threads, never on a UI thread. Frontends are
responsible for marshalling them (see ``ffsubsyncui.qt.bridge``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass
import threading

from ffsubsyncui.core.config import AppConfig
from ffsubsyncui.core.engine import EngineError, SyncProcess, build_spec, validate
from ffsubsyncui.core.models import JobStatus, SyncJob, SyncResult
from ffsubsyncui.core.speech_cache import SpeechCache
from ffsubsyncui.core.store import JobStore

_STAGE_BY_STATUS = {
    JobStatus.SUCCEEDED: "Complete",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
    JobStatus.SKIPPED_LOW_QUALITY: "Left unmodified",
}


@dataclass
class QueueCallbacks:
    """Observers for queue activity. All invoked from worker threads."""

    job_changed: Callable[[SyncJob], None] | None = None
    progress: Callable[[str, float | None, str], None] | None = None
    log: Callable[[str, str, str], None] | None = None
    running_changed: Callable[[bool], None] | None = None
    queue_changed: Callable[[], None] | None = None


@dataclass(slots=True)
class QueueCounts:
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    ready: int = 0

    @property
    def total(self) -> int:
        return (
            self.queued
            + self.running
            + self.succeeded
            + self.failed
            + self.cancelled
            + self.skipped
        )


class SyncQueue:
    def __init__(
        self,
        store: JobStore,
        config: AppConfig,
        speech_cache: SpeechCache,
        callbacks: QueueCallbacks | None = None,
        *,
        persist_queue: bool = True,
    ) -> None:
        self._store = store
        self._config = config
        self._cache = speech_cache
        self._callbacks = callbacks or QueueCallbacks()
        # The CLI shares the database for history but must not overwrite the
        # GUI's stored queue, since save_jobs replaces the table wholesale.
        self._persist_queue = persist_queue

        self._lock = threading.RLock()
        self._jobs: list[SyncJob] = store.load_jobs() if persist_queue else []
        self._processes: dict[str, SyncProcess] = {}
        self._workers: list[threading.Thread] = []
        self._active_count = 0
        self._draining = False
        self._shutdown = False

    # -- inspection ------------------------------------------------------

    @property
    def callbacks(self) -> QueueCallbacks:
        return self._callbacks

    @callbacks.setter
    def callbacks(self, value: QueueCallbacks) -> None:
        """Swap observers wholesale; used to detach a closing window."""
        self._callbacks = value

    def snapshot(self) -> list[SyncJob]:
        with self._lock:
            return list(self._jobs)

    def find(self, job_id: str) -> SyncJob | None:
        with self._lock:
            return next((job for job in self._jobs if job.id == job_id), None)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active_count > 0

    def counts(self) -> QueueCounts:
        counts = QueueCounts()
        with self._lock:
            for job in self._jobs:
                match job.status:
                    case JobStatus.QUEUED:
                        counts.queued += 1
                        if job.enabled:
                            counts.ready += 1
                    case JobStatus.RUNNING:
                        counts.running += 1
                    case JobStatus.SUCCEEDED:
                        counts.succeeded += 1
                    case JobStatus.FAILED:
                        counts.failed += 1
                    case JobStatus.CANCELLED:
                        counts.cancelled += 1
                    case JobStatus.SKIPPED_LOW_QUALITY:
                        counts.skipped += 1
        return counts

    # -- mutation --------------------------------------------------------

    def add(self, jobs: Iterable[SyncJob]) -> int:
        additions = list(jobs)
        if not additions:
            return 0
        with self._lock:
            self._jobs.extend(additions)
            self._persist()
        self._notify_queue_changed()
        if self._draining:
            self._ensure_workers()
        return len(additions)

    def remove(self, job_ids: Iterable[str]) -> int:
        wanted = set(job_ids)
        with self._lock:
            # A running job must be cancelled before it can be removed.
            removable = {
                job.id
                for job in self._jobs
                if job.id in wanted and job.status is not JobStatus.RUNNING
            }
            if not removable:
                return 0
            self._jobs = [job for job in self._jobs if job.id not in removable]
            self._persist()
        self._notify_queue_changed()
        return len(removable)

    def retry(self, job_ids: Iterable[str]) -> int:
        wanted = set(job_ids)
        changed: list[SyncJob] = []
        with self._lock:
            for job in self._jobs:
                if job.id in wanted and job.status.is_retryable:
                    job.reset_for_retry()
                    changed.append(job)
            if changed:
                self._persist()
        for job in changed:
            self._emit_job_changed(job)
        if changed:
            self._notify_queue_changed()
            if self._draining:
                self._ensure_workers()
        return len(changed)

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None or job.status is not JobStatus.QUEUED:
                return False
            job.enabled = enabled
            self._persist()
        self._emit_job_changed(job)
        self._notify_queue_changed()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._jobs)
            self._jobs = [job for job in self._jobs if not job.status.is_terminal]
            removed = before - len(self._jobs)
            if removed:
                self._persist()
        if removed:
            self._notify_queue_changed()
        return removed

    # -- execution -------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._draining = True
        self._ensure_workers()

    def stop(self) -> None:
        """Stop after the current jobs; do not start anything new."""
        with self._lock:
            self._draining = False
        if not self.is_running:
            self._emit_running(False)

    def cancel(self, job_id: str) -> bool:
        """Kill one running job."""
        with self._lock:
            process = self._processes.get(job_id)
        if process is None:
            return False
        process.cancel()
        return True

    def cancel_all(self) -> int:
        with self._lock:
            self._draining = False
            processes = list(self._processes.values())
        for process in processes:
            process.cancel()
        return len(processes)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._shutdown = True
            self._draining = False
            workers = list(self._workers)
        self.cancel_all()
        for worker in workers:
            worker.join(timeout=timeout)
        with self._lock:
            self._persist()

    # -- internals -------------------------------------------------------

    def _ensure_workers(self) -> None:
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            wanted = max(1, min(self._config.concurrency, len(self._runnable())))
            missing = wanted - len(self._workers)
            for _ in range(max(0, missing)):
                worker = threading.Thread(
                    target=self._worker_loop, name="ffsubsync-worker", daemon=True
                )
                self._workers.append(worker)
                worker.start()

    def _runnable(self) -> list[SyncJob]:
        return [
            job for job in self._jobs if job.enabled and job.status is JobStatus.QUEUED
        ]

    def _claim(self) -> SyncJob | None:
        with self._lock:
            if not self._draining or self._shutdown:
                return None
            job = next(iter(self._runnable()), None)
            if job is None:
                return None
            job.status = JobStatus.RUNNING
            job.stage = "Starting"
            job.progress = None
            job.last_error = ""
            self._active_count += 1
            was_idle = self._active_count == 1
            self._persist()
        if was_idle:
            self._emit_running(True)
        self._emit_job_changed(job)
        return job

    def _worker_loop(self) -> None:
        while True:
            job = self._claim()
            if job is None:
                break
            result = self._execute(job)
            with self._lock:
                job.result = result
                job.status = result.status
                job.progress = 1.0 if result.success else None
                job.stage = _STAGE_BY_STATUS.get(result.status, "Finished")
                job.last_error = result.error
                self._processes.pop(job.id, None)
                self._active_count -= 1
                idle = self._active_count == 0
                self._persist()
            with contextlib.suppress(Exception):
                self._store.record(job, result)
            self._emit_job_changed(job)
            self._notify_queue_changed()
            if idle:
                # Another worker may still pick something up; only report idle
                # once nothing is left to claim.
                with self._lock:
                    finished = not self._runnable() or not self._draining
                if finished:
                    self._emit_running(False)

    def _execute(self, job: SyncJob) -> SyncResult:
        try:
            validate(job)
        except EngineError as exc:
            return SyncResult(status=JobStatus.FAILED, return_code=1, error=str(exc))

        reference_override = None
        serialize = False
        used_cache = False
        group_key = job.group_key

        if self._config.speech_cache_enabled and group_key:
            hit = self._cache.lookup(job.reference_path, job.options)
            if hit is not None:
                reference_override = hit
                used_cache = True
            elif self._cache.claim_producer(group_key):
                serialize = True

        process = SyncProcess(
            build_spec(
                job,
                reference_override=reference_override,
                serialize_speech=serialize,
            )
        )
        with self._lock:
            self._processes[job.id] = process

        try:
            result = process.run(
                on_progress=lambda fraction, stage: self._emit_progress(
                    job.id, fraction, stage
                ),
                on_log=lambda level, message: self._emit_log(job.id, level, message),
            )
        finally:
            if serialize and group_key:
                # Harvest even on failure so no stray .npz is left beside media.
                with contextlib.suppress(Exception):
                    self._cache.harvest(job.reference_path, job.options)
                self._cache.release_producer(group_key)

        result.used_speech_cache = used_cache
        return result

    def _persist(self) -> None:
        if not self._persist_queue:
            return
        with contextlib.suppress(Exception):
            self._store.save_jobs(self._jobs)

    def _emit_job_changed(self, job: SyncJob) -> None:
        if self._callbacks.job_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.job_changed(job)

    def _emit_progress(self, job_id: str, fraction: float | None, stage: str) -> None:
        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is not None:
                job.progress = fraction
                job.stage = stage
        if self._callbacks.progress is not None:
            with contextlib.suppress(Exception):
                self._callbacks.progress(job_id, fraction, stage)

    def _emit_log(self, job_id: str, level: str, message: str) -> None:
        if self._callbacks.log is not None:
            with contextlib.suppress(Exception):
                self._callbacks.log(job_id, level, message)

    def _emit_running(self, running: bool) -> None:
        if self._callbacks.running_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.running_changed(running)

    def _notify_queue_changed(self) -> None:
        if self._callbacks.queue_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.queue_changed()
