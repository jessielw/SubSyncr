"""The job queue: a thread pool of cancellable ffsubsync subprocesses.

Work is claimed a *group* at a time - one reference and every subtitle that
syncs against it - so the expensive speech extraction is paid once per video
rather than once per subtitle.

Callbacks fire on worker threads, never on a UI thread. Frontends are
responsible for marshalling them (see ``subsyncr.qt.bridge``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass
import json
import threading

from subsyncr.core.config import AppConfig
from subsyncr.core.engine import (
    EngineError,
    SyncProcess,
    build_spec,
    can_run,
    validate,
)
from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, SyncJob, SyncResult
from subsyncr.core.scanner import needs_ffmpeg
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore

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

    @property
    def finished(self) -> int:
        return self.succeeded + self.failed + self.cancelled + self.skipped


def _batch_key(job: SyncJob) -> str:
    """Jobs sharing this key can share one worker process and one extraction.

    The reference and every option that shapes the run must match, since the
    group is executed from a single ffsubsync namespace.
    """
    if not job.group_key:
        # No group means run alone; the job id keeps it unique.
        return f"solo:{job.id}"
    # Only the settings this run actually uses; an idle engine's options must
    # not split a group.
    options = json.dumps(job.options.identity(), sort_keys=True)
    return f"{job.group_key}|{job.reference_path}|{options}"


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
        # Every job in a running group maps to that group's single process.
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

    def pending_groups(self) -> int:
        """How many distinct worker processes the remaining work needs."""
        with self._lock:
            return len({_batch_key(job) for job in self._runnable()})

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
        """Stop after the current groups; do not start anything new."""
        with self._lock:
            self._draining = False
        if not self.is_running:
            self._emit_running(False)

    def cancel(self, job_id: str) -> bool:
        """Kill the worker running this job.

        Siblings sharing the job's process stop too: they are one ffsubsync run
        over one shared extraction and cannot be separated. Whatever already
        finished keeps its result; the rest are reported as cancelled.
        """
        with self._lock:
            process = self._processes.get(job_id)
        if process is None:
            return False
        process.cancel()
        return True

    def cancel_all(self) -> int:
        with self._lock:
            self._draining = False
            processes = list(dict.fromkeys(self._processes.values()))
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
            # One thread per pending group, capped by the concurrency setting.
            groups = len({_batch_key(job) for job in self._runnable()})
            wanted = max(1, min(self._config.concurrency, groups))
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

    def _claim_group(self) -> list[SyncJob]:
        """Take every runnable job that shares the next group's key."""
        with self._lock:
            if not self._draining or self._shutdown:
                return []
            runnable = self._runnable()
            if not runnable:
                return []
            key = _batch_key(runnable[0])
            group = [job for job in runnable if _batch_key(job) == key]
            for job in group:
                job.status = JobStatus.RUNNING
                job.stage = "Starting"
                job.progress = None
                job.last_error = ""
            self._active_count += 1
            was_idle = self._active_count == 1
            self._persist()
        if was_idle:
            self._emit_running(True)
        for job in group:
            self._emit_job_changed(job)
        return group

    def _worker_loop(self) -> None:
        while True:
            group = self._claim_group()
            if not group:
                break
            results = self._execute(group)
            with self._lock:
                for job in group:
                    result = results.get(
                        job.id,
                        SyncResult(
                            status=JobStatus.FAILED,
                            return_code=1,
                            error="the sync worker reported no result for this job",
                        ),
                    )
                    job.result = result
                    job.status = result.status
                    job.progress = 1.0 if result.success else None
                    job.stage = _STAGE_BY_STATUS.get(result.status, "Finished")
                    job.last_error = result.error
                    self._processes.pop(job.id, None)
                self._active_count -= 1
                idle = self._active_count == 0
                self._persist()
            for job in group:
                with contextlib.suppress(Exception):
                    self._store.record(job, job.result or SyncResult(job.status))
                self._emit_job_changed(job)
            self._notify_queue_changed()
            if idle:
                # Another worker may still pick something up; only report idle
                # once nothing is left to claim.
                with self._lock:
                    finished = not self._runnable() or not self._draining
                if finished:
                    self._emit_running(False)

    def _execute(self, group: list[SyncJob]) -> dict[str, SyncResult]:
        # Validation is per job: one bad subtitle should not sink its siblings.
        runnable: list[SyncJob] = []
        results: dict[str, SyncResult] = {}
        for job in group:
            try:
                validate(job)
            except EngineError as exc:
                results[job.id] = SyncResult(
                    status=JobStatus.FAILED, return_code=1, error=str(exc)
                )
            else:
                runnable.append(job)
        if not runnable:
            return results

        # The configured engine first, then whatever the user lined up behind
        # it. A job only moves down the chain if it failed or the engine
        # refused to trust its own answer.
        pending = list(runnable)
        options = runnable[0].options
        previous = options.engine
        for position, engine in enumerate((options.engine, *options.fallback_chain)):
            if not pending:
                break
            capable = [job for job in pending if can_run(job, engine)]
            if not capable:
                continue
            if position:
                self._announce_fallback(capable, previous, engine)
            produced = self._attempt(capable, engine, runnable)
            results.update(produced)
            attempted = {job.id for job in capable}
            pending = [
                job
                for job in pending
                if job.id not in attempted
                # Never drag a job the user cancelled onto another engine.
                or produced[job.id].status
                in (JobStatus.FAILED, JobStatus.SKIPPED_LOW_QUALITY)
            ]
            previous = engine
        return results

    def _announce_fallback(
        self, jobs: list[SyncJob], previous: EngineId, engine: EngineId
    ) -> None:
        for job in jobs:
            self._emit_log(
                job.id,
                "INFO",
                f"{previous} did not settle it; retrying with {engine}",
                jobs,
            )

    def _attempt(
        self, runnable: list[SyncJob], engine: EngineId, group: list[SyncJob]
    ) -> dict[str, SyncResult]:
        """One pass over these jobs with one engine."""
        first = runnable[0]
        reference_override = None
        serialize = False
        used_cache = False
        producer_key = ""

        # The .npz cache holds ffsubsync speech arrays and means nothing to the
        # other engines. A subtitle reference has no expensive extraction either.
        caches_speech = (
            self._config.speech_cache_enabled
            and engine is EngineId.FFSUBSYNC
            and needs_ffmpeg(first.reference_path)
        )
        if caches_speech:
            speech_options = first.options.ffsubsync
            hit = self._cache.lookup(first.reference_path, speech_options)
            if hit is not None:
                reference_override = hit
                used_cache = True
            else:
                # Only one process may write the sidecar for a given reference.
                producer_key = str(
                    self._cache.cached_path(first.reference_path, speech_options)
                )
                serialize = self._cache.claim_producer(producer_key)

        process = SyncProcess(
            build_spec(
                runnable,
                reference_override=reference_override,
                serialize_speech=serialize,
                engine=engine,
            )
        )
        with self._lock:
            for job in runnable:
                self._processes[job.id] = process

        try:
            produced = process.run(
                on_progress=lambda job_id, fraction, stage: self._emit_progress(
                    job_id, fraction, stage, group
                ),
                on_log=lambda job_id, level, message: self._emit_log(
                    job_id, level, message, group
                ),
            )
        finally:
            if serialize and producer_key:
                # Harvest even on failure so no stray .npz is left beside media.
                with contextlib.suppress(Exception):
                    self._cache.harvest(first.reference_path, first.options.ffsubsync)
                self._cache.release_producer(producer_key)

        for result in produced.values():
            result.used_speech_cache = used_cache
        return produced

    def _persist(self) -> None:
        if not self._persist_queue:
            return
        with contextlib.suppress(Exception):
            self._store.save_jobs(self._jobs)

    def _emit_job_changed(self, job: SyncJob) -> None:
        if self._callbacks.job_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.job_changed(job)

    def _emit_progress(
        self,
        job_id: str | None,
        fraction: float | None,
        stage: str,
        group: list[SyncJob],
    ) -> None:
        # A group-wide event (the shared extraction) applies to every member.
        targets = [job.id for job in group] if job_id is None else [job_id]
        with self._lock:
            for job in self._jobs:
                if job.id in targets and job.status is JobStatus.RUNNING:
                    job.progress = fraction
                    job.stage = stage
        if self._callbacks.progress is not None:
            for target in targets:
                with contextlib.suppress(Exception):
                    self._callbacks.progress(target, fraction, stage)

    def _emit_log(
        self, job_id: str | None, level: str, message: str, group: list[SyncJob]
    ) -> None:
        if self._callbacks.log is None:
            return
        # Attribute group-wide lines to the job that is paying for them.
        target = job_id if job_id is not None else group[0].id
        with contextlib.suppress(Exception):
            self._callbacks.log(target, level, message)

    def _emit_running(self, running: bool) -> None:
        if self._callbacks.running_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.running_changed(running)

    def _notify_queue_changed(self) -> None:
        if self._callbacks.queue_changed is not None:
            with contextlib.suppress(Exception):
                self._callbacks.queue_changed()
