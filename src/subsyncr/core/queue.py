"""The job queue: a thread pool of cancellable ffsubsync subprocesses.

Work is claimed a *group* at a time - one reference and every subtitle that
syncs against it - so the expensive speech extraction is paid once per video
rather than once per subtitle.

Callbacks fire on worker threads, never on a UI thread. Frontends are
responsible for marshalling them (see ``subsyncr.qt.bridge``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import contextlib
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time

from subsyncr.core import logger, reference
from subsyncr.core.config import AppConfig
from subsyncr.core.embedded import Track
from subsyncr.core.engine import (
    EngineError,
    SyncProcess,
    build_spec,
    can_run,
    validate,
)
from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, ReferenceTrack, SyncJob, SyncResult
from subsyncr.core.scanner import needs_ffmpeg
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore

_log = logging.getLogger(__name__)

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
    # Two subtitles on one video can want different tracks (one per language),
    # and those are different extractions.
    track = job.reference_track.token if job.reference_track else ""
    return f"{job.group_key}|{job.reference_path}|{options}|{track}"


def _cache_source(job: SyncJob) -> str:
    """What, beyond the speech options, the cached array was built from."""
    if job.reference_track is not None:
        return job.reference_track.token
    # Unresolved: the worker's own rules decide, and extraction changes the
    # array entirely, so at least keep the two apart.
    return "embedded" if job.options.extract_embedded else ""


def _log_outcome(job: SyncJob) -> None:
    result = job.result
    name = job.subtitle_path.name
    if job.status is JobStatus.SUCCEEDED and result is not None:
        offset = (
            f", offset {result.offset_seconds:+.3f}s"
            if result.offset_seconds is not None
            else ""
        )
        _log.info(
            "%s: synced with %s%s -> %s",
            name,
            result.engine or "unknown engine",
            offset,
            job.effective_output,
        )
    elif job.status is JobStatus.FAILED:
        _log.warning("%s: failed: %s", name, job.last_error or "no reason given")
    else:
        _log.info("%s: %s", name, job.status.value)


class _PathIdentities:
    """``same_path`` for many comparisons, reading each path from disk once.

    The conflict check compares every output with three paths of every other
    queued job; calling ``same_path`` per pair resolved and stat'ed the same
    files over and over, which on a network share added up to seconds.
    """

    def __init__(self) -> None:
        self._resolved: dict[Path, Path] = {}
        self._stats: dict[Path, os.stat_result | None] = {}

    def same(self, left: Path, right: Path) -> bool:
        if self._resolve(left) == self._resolve(right):
            return True
        first, second = self._stat(left), self._stat(right)
        # What Path.samefile does, minus the repeat stat calls.
        return (
            first is not None and second is not None and os.path.samestat(first, second)
        )

    def _resolve(self, path: Path) -> Path:
        resolved = self._resolved.get(path)
        if resolved is None:
            resolved = self._resolved[path] = path.resolve()
        return resolved

    def _stat(self, path: Path) -> os.stat_result | None:
        if path not in self._stats:
            try:
                self._stats[path] = path.stat()
            except OSError:
                self._stats[path] = None
        return self._stats[path]


def _crash_results(group: list[SyncJob], exc: Exception) -> dict[str, SyncResult]:
    """The outcome for a group whose run raised something nobody expected."""
    return {
        job.id: SyncResult(
            status=JobStatus.FAILED,
            return_code=1,
            error=f"the queue hit an internal error: {type(exc).__name__}: {exc}",
        )
        for job in group
    }


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
        # Saving rewrites the whole table, which takes a while on a long queue.
        # It happens after the queue lock is released, so the GUI thread is
        # never left waiting on SQLite: ``_persist`` asks for a save under the
        # lock, ``_flush_persist`` writes the newest snapshot outside it.
        self._persist_lock = threading.Lock()
        self._persist_wanted = 0
        self._persist_written = 0
        # Stopping a worker can take seconds (see SyncProcess.cancel), so it
        # happens on these threads rather than the caller's.
        self._cancellers: list[threading.Thread] = []
        # Every job in a running group maps to that group's single process.
        self._processes: dict[str, SyncProcess] = {}
        self._workers: list[threading.Thread] = []
        self._active_count = 0
        self._draining = False
        self._shutdown = False
        # Reference tracks are settled before a job can be claimed. One
        # resolver at a time, so a video is never probed twice concurrently.
        self._probe = reference.TrackProbe()
        self._resolve_lock = threading.Lock()
        # Jobs whose PGS setting is an ffmpeg specifier we cannot map; the
        # worker hands those to ffsubsync as they are.
        self._unresolvable: set[str] = set()
        self._probers: list[threading.Thread] = []
        if self._jobs:
            self._resolve_in_background()

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

    def tracks_for(self, video: Path, ffmpeg_path: str = "") -> list[Track] | None:
        """Every subtitle track in a reference, or None if it can't be read.

        Runs ffprobe when the video has not been read yet, so keep it off the
        GUI thread; ``cached_tracks`` is the non-blocking way to ask.
        """
        if not needs_ffmpeg(video):
            return []
        return self._probe.tracks(video, ffmpeg_path)

    def cached_tracks(self, video: Path) -> list[Track] | None:
        """The tracks already read for this video, or None if not read yet.

        Never touches the file, so it is safe to call from the GUI thread.
        """
        if not needs_ffmpeg(video):
            return []
        return self._probe.peek(video)

    def resolving(self, job: SyncJob) -> bool:
        """Whether this job's track is still waiting to be read."""
        with self._lock:
            return self._needs_resolving(job)

    def pending_groups(self) -> int:
        """How many distinct worker processes the remaining work needs."""
        with self._lock:
            return len({_batch_key(job) for job in self._runnable()})

    # -- mutation --------------------------------------------------------

    def add(self, jobs: Iterable[SyncJob]) -> int:
        additions = list(jobs)
        if not additions:
            return 0
        with self._editing():
            self._jobs.extend(additions)
            self._persist()
        self._notify_queue_changed()
        self._resolve_in_background()
        if self._draining:
            self._ensure_workers()
        return len(additions)

    def set_reference(
        self, job_ids: Iterable[str], track: ReferenceTrack | None
    ) -> int:
        """Pin what these jobs align against; None goes back to automatic.

        A failed, cancelled or low-quality job is queued again, since a new
        reference only means anything if it runs again. Running and succeeded
        jobs are left alone: one is mid-flight, the other is final.
        """
        wanted = set(job_ids)
        changed: list[SyncJob] = []
        with self._editing():
            for job in self._jobs:
                if job.id not in wanted or job.status in (
                    JobStatus.RUNNING,
                    JobStatus.SUCCEEDED,
                ):
                    continue
                job.reference_track = track
                self._unresolvable.discard(job.id)
                if job.status.is_retryable:
                    job.reset_for_retry()
                changed.append(job)
            if changed:
                self._persist()
        for job in changed:
            self._emit_job_changed(job)
        if changed:
            self._notify_queue_changed()
            if track is None:
                self._resolve_in_background()
            if self._draining:
                self._ensure_workers()
        return len(changed)

    def remove(self, job_ids: Iterable[str]) -> int:
        wanted = set(job_ids)
        with self._editing():
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
        with self._editing():
            for job in self._jobs:
                if job.id in wanted and job.status.is_retryable:
                    job.reset_for_retry()
                    # The video may be readable now, if it was not before.
                    self._unresolvable.discard(job.id)
                    changed.append(job)
            if changed:
                self._persist()
        for job in changed:
            self._emit_job_changed(job)
        if changed:
            self._notify_queue_changed()
            self._resolve_in_background()
            if self._draining:
                self._ensure_workers()
        return len(changed)

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._editing():
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None or job.status is not JobStatus.QUEUED:
                return False
            job.enabled = enabled
            self._persist()
        self._emit_job_changed(job)
        self._notify_queue_changed()
        return True

    def clear_finished(self) -> int:
        with self._editing():
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
        self._stop_processes([process])
        return True

    def cancel_all(self) -> int:
        """Ask every running worker to stop. Returns without waiting for them."""
        with self._lock:
            self._draining = False
            processes = list(dict.fromkeys(self._processes.values()))
        self._stop_processes(processes)
        return len(processes)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Cancel everything and wait, up to ``timeout`` seconds in all."""
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._shutdown:
                return  # Already done; the store may be closed by now.
            self._shutdown = True
            self._draining = False
            workers = list(self._workers)
            probers = list(self._probers)
        self.cancel_all()
        with self._lock:
            cancellers = list(self._cancellers)
        # Cancellers first: they are what makes the workers return.
        for thread in (*cancellers, *workers, *probers):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._persist()
        self._flush_persist(final=True)

    # -- internals -------------------------------------------------------

    def _stop_processes(self, processes: Iterable[SyncProcess]) -> None:
        """Cancel workers in parallel, off the calling thread.

        ``SyncProcess.cancel`` waits for the child to exit and escalates to
        terminate and kill: seconds per worker in the worst case. Run from a
        button on the GUI thread, that froze the window for all of it.
        """
        started: list[threading.Thread] = []
        for process in processes:
            thread = threading.Thread(
                target=process.cancel, name="subsyncr-cancel", daemon=True
            )
            thread.start()
            started.append(thread)
        if started:
            with self._lock:
                self._cancellers = [
                    thread for thread in self._cancellers if thread.is_alive()
                ]
                self._cancellers.extend(started)

    @contextlib.contextmanager
    def _editing(self) -> Iterator[None]:
        """Hold the queue lock for a change, then save once it is released."""
        with self._lock:
            yield
        self._flush_persist()

    def _ensure_workers(self) -> None:
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            # One thread per pending group, capped by the concurrency setting.
            groups = len({_batch_key(job) for job in self._runnable()})
            wanted = max(1, min(self._config.concurrency, groups))
            missing = wanted - len(self._workers)
            for _ in range(max(0, missing)):
                worker = threading.Thread(
                    target=self._worker_loop, name="subsyncr-worker", daemon=True
                )
                self._workers.append(worker)
                worker.start()

    def _runnable(self) -> list[SyncJob]:
        return [
            job for job in self._jobs if job.enabled and job.status is JobStatus.QUEUED
        ]

    def _needs_resolving(self, job: SyncJob) -> bool:
        return (
            job.reference_track is None
            and job.status is JobStatus.QUEUED
            and job.id not in self._unresolvable
            and needs_ffmpeg(job.reference_path)
        )

    def _resolve_pending(self) -> None:
        """Settle every queued job's reference track that is still open.

        Probing happens outside the queue lock - it is a subprocess - and the
        answer is only written back if nobody chose a track in the meantime.
        """
        changed: list[SyncJob] = []
        with self._resolve_lock:
            with self._lock:
                todo = [job for job in self._jobs if self._needs_resolving(job)]
            for job in todo:
                tracks = self._probe.tracks(
                    job.reference_path, job.options.environment.ffmpeg_path
                )
                choice = (
                    None
                    if tracks is None
                    else reference.choose(tracks, job.options, job.language)
                )
                with self._lock:
                    if self._shutdown:
                        return
                    if not self._needs_resolving(job):
                        continue
                    if tracks is None:
                        # Saving "audio" here would quietly drop PGS for good;
                        # leave it open, and the worker reads the file itself.
                        self._unresolvable.add(job.id)
                        _log.info(
                            "%s: could not read its subtitle tracks yet;"
                            " the track is picked when it runs",
                            job.reference_path.name,
                        )
                        continue
                    if choice is None:
                        self._unresolvable.add(job.id)
                        continue
                    job.reference_track = choice
                    changed.append(job)
            if changed:
                with self._editing():
                    if self._shutdown:
                        return  # shutdown() has saved, and the store may be closed.
                    self._persist()
        for job in changed:
            self._emit_job_changed(job)

    def _resolve_in_background(self) -> None:
        def run() -> None:
            try:
                self._resolve_pending()
            except Exception:
                _log.exception("could not probe reference tracks")
            # A worker that found only unresolved jobs will have exited.
            with self._lock:
                draining = self._draining and not self._shutdown
            if draining:
                self._ensure_workers()

        thread = threading.Thread(target=run, name="subsyncr-probe", daemon=True)
        with self._lock:
            if self._shutdown:
                return
            self._probers = [item for item in self._probers if item.is_alive()]
            self._probers.append(thread)
        thread.start()

    def _claim_group(self) -> list[SyncJob]:
        """Take every runnable job that shares the next group's key."""
        with self._editing():
            if not self._draining or self._shutdown:
                return []
            # A job whose track is still being probed is not ready to group.
            runnable = [
                job for job in self._runnable() if not self._needs_resolving(job)
            ]
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
            self._resolve_pending()
            group = self._claim_group()
            if not group:
                break
            try:
                results = self._execute(group)
            except Exception as exc:
                # Without this the thread dies with the group still marked
                # Running, and the queue sits there looking busy for the rest
                # of the session. Fail them first, then let the exception go
                # on to the thread's excepthook - a bug in here is worth
                # reporting, not swallowing.
                self._finish(group, _crash_results(group, exc))
                raise
            self._finish(group, results)

    def _finish(self, group: list[SyncJob], results: dict[str, SyncResult]) -> None:
        """Write a group's outcome back and tell everyone who is listening."""
        with self._editing():
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
            _log_outcome(job)
            try:
                self._store.record(job, job.result or SyncResult(job.status))
            except Exception:
                _log.exception("could not record %s in history", job.subtitle_path)
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
        # Inspect the whole queue, including other running groups, before any
        # worker can publish. Ambiguous destinations require user correction;
        # replace_output only authorizes replacing an existing output, not
        # throwing away a different job's result or input.
        # The snapshot is taken under the lock; the comparison is not. It
        # touches the disk for every path, and done under the lock it held up
        # the GUI thread (which reads the queue to paint) for the whole scan.
        with self._lock:
            candidates = {job.id: job for job in [*self._jobs, *group]}
            active = [
                (
                    job.id,
                    (job.effective_output, job.subtitle_path, job.reference_path),
                )
                for job in candidates.values()
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            ]
            outputs = [(job.id, job.effective_output) for job in group]
        conflicts: dict[str, str] = {}
        identify = _PathIdentities()
        for job_id, output in outputs:
            for other_id, paths in active:
                if job_id == other_id:
                    continue
                if any(identify.same(output, path) for path in paths):
                    conflicts[job_id] = (
                        "Output conflicts with another queued job: "
                        f"{output}. Choose distinct output names "
                        "or remove the conflicting job."
                    )
                    break
        for job in group:
            try:
                if job.id in conflicts:
                    raise EngineError(conflicts[job.id])
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
        # Whether ``previous`` actually ran, or was skipped as unable.
        previous_ran = False
        for position, engine in enumerate((options.engine, *options.fallback_chain)):
            if not pending:
                break
            capable = [job for job in pending if can_run(job, engine)]
            if not capable:
                continue
            if position:
                self._announce_fallback(capable, previous, engine, ran=previous_ran)
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
            previous_ran = True
        return results

    def _announce_fallback(
        self, jobs: list[SyncJob], previous: EngineId, engine: EngineId, *, ran: bool
    ) -> None:
        message = (
            f"{previous} did not settle it; retrying with {engine}"
            if ran
            else f"{previous} cannot handle these files; using {engine}"
        )
        for job in jobs:
            self._emit_log(job.id, "INFO", message, jobs)

    def _attempt(
        self, runnable: list[SyncJob], engine: EngineId, group: list[SyncJob]
    ) -> dict[str, SyncResult]:
        """One pass over these jobs with one engine."""
        first = runnable[0]
        reference_override = None
        serialize = False
        used_cache = False
        producer_key = ""
        speech_output: Path | None = None

        # The .npz cache holds ffsubsync speech arrays and means nothing to the
        # other engines. A subtitle reference has no expensive extraction either.
        caches_speech = (
            self._config.speech_cache_enabled
            and engine is EngineId.FFSUBSYNC
            and needs_ffmpeg(first.reference_path)
        )
        source = _cache_source(first)
        if caches_speech:
            speech_options = first.options.ffsubsync
            hit = self._cache.lookup(first.reference_path, speech_options, source)
            if hit is not None:
                reference_override = hit
                used_cache = True
            else:
                # One producer per cache key; each owns a distinct staging file.
                producer_key = str(
                    self._cache.cached_path(
                        first.reference_path, speech_options, source
                    )
                )
                serialize = self._cache.claim_producer(producer_key)
                if serialize:
                    try:
                        speech_output = self._cache.stage()
                    except OSError:
                        self._cache.release_producer(producer_key)
                        serialize = False
                        _log.warning(
                            "Speech cache is not writable; syncing without caching"
                        )

        process = SyncProcess(
            build_spec(
                runnable,
                reference_override=reference_override,
                serialize_speech=serialize,
                speech_output=speech_output,
                engine=engine,
            )
        )
        with self._lock:
            for job in runnable:
                self._processes[job.id] = process
        _log.info(
            "%s: %s on %d subtitle(s)%s",
            first.reference_path.name,
            engine,
            len(runnable),
            " (cached speech)" if used_cache else "",
        )

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
            if serialize and producer_key and speech_output is not None:
                # Even failure cleanup can touch only our own staging file.
                with contextlib.suppress(Exception):
                    self._cache.harvest(
                        first.reference_path,
                        first.options.ffsubsync,
                        speech_output,
                        source,
                    )
                self._cache.release_producer(producer_key)

        for result in produced.values():
            result.used_speech_cache = used_cache
        return produced

    def _persist(self) -> None:
        """Ask for the queue to be saved. Call with the queue lock held.

        The write itself happens in ``_flush_persist`` once the lock is
        released; ``_editing`` does both.
        """
        if self._persist_queue:
            self._persist_wanted += 1

    def _flush_persist(self, *, final: bool = False) -> None:
        """Write the newest snapshot, unless something newer already landed.

        The snapshot is taken under the queue lock, which is then released
        before the write lock is taken, and the write never takes the queue
        lock - so the two cannot deadlock, and a writer holding an older
        snapshot skips its write once a newer one is saved.
        """
        if not self._persist_queue:
            return
        with self._lock:
            if self._shutdown and not final:
                return  # shutdown() saves last; the store may already be closed.
            generation = self._persist_wanted
            if generation <= self._persist_written:
                return
            jobs = list(self._jobs)
        with self._persist_lock:
            if generation <= self._persist_written:
                return
            try:
                self._store.save_jobs(jobs)
            except Exception:
                _log.exception("could not save the queue")
            else:
                self._persist_written = generation

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
        # Attribute group-wide lines to the job that is paying for them.
        target = job_id if job_id is not None else group[0].id
        # The file names the subtitle; the GUI shows the line under its job.
        subject = next(
            (job.subtitle_path.name for job in group if job.id == target), target
        )
        logger.forward(level, f"{subject}: {message}")
        if self._callbacks.log is None:
            return
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
