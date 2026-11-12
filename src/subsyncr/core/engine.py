"""Parent side of the sync subprocess: spawn, stream events, cancel.

The unit of work is a *group*: one reference file and every subtitle that syncs
against it. One process handles the whole group so the expensive speech
extraction is paid once and shared in memory.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import IO, Any

from subsyncr.core import engines, logger, subtitles
from subsyncr.core.engines import EngineId
from subsyncr.core.ffmpeg import probe
from subsyncr.core.models import JobStatus, SyncJob, SyncResult
from subsyncr.core.output import same_path
from subsyncr.core.scanner import SUBTITLE_REFERENCE_EXTENSIONS, needs_ffmpeg
from subsyncr.core.spawn import creation_flags

# job_id (None for group-wide events), fraction, stage
ProgressCallback = Callable[[str | None, float | None, str], None]
# job_id (None for group-wide events), level, message
LogCallback = Callable[[str | None, str, str], None]

# Seconds to let the worker stop cleanly after stdin closes, before
# escalating to terminate.
_CANCEL_GRACE = 2.0

# Seconds to wait for a terminated worker to exit before killing it outright.
_KILL_GRACE = 5.0

# Keep the tail of the child's stderr for diagnostics without unbounded growth.
_STDERR_TAIL = 50

_STATUS_BY_NAME = {
    "succeeded": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "skipped_low_quality": JobStatus.SKIPPED_LOW_QUALITY,
    "cancelled": JobStatus.CANCELLED,
}


class EngineError(RuntimeError):
    """A job could not safely be started."""


def worker_command() -> list[str]:
    """How to invoke the sync child in this deployment."""
    if getattr(sys, "frozen", False):
        # The packaged executable re-execs itself; see subsyncr.__main__.
        return [sys.executable, "--sync-worker"]
    return [sys.executable, "-m", "subsyncr.core.worker"]


def capability_problem(job: SyncJob, engine: EngineId | None = None) -> str:
    """Why this engine cannot handle these files, or "" if it can.

    Shared by validation and the fallback chain: there is no point spending a
    process on autosubsync for a subtitle reference it cannot read, and the
    chain needs to know that without trying.
    """
    spec = engines.get(engine or job.options.engine)
    suffix = job.reference_path.suffix.lower()
    is_subtitle = suffix in SUBTITLE_REFERENCE_EXTENSIONS
    if not spec.accepts_reference(suffix, is_subtitle=is_subtitle):
        return f"{spec.label} cannot use {job.reference_path.name} as a reference"
    # A format the engine cannot read is fine as long as we can convert it on
    # the way in. Only something that is neither is a dead end.
    readable = spec.accepts_subtitle(job.subtitle_path.suffix)
    if not readable and not subtitles.convertible(job.subtitle_path):
        return f"{spec.label} cannot read {job.subtitle_path.suffix} subtitles"
    return ""


def can_run(job: SyncJob, engine: EngineId) -> bool:
    return not capability_problem(job, engine)


def first_capable(job: SyncJob) -> EngineId | None:
    """The engine that will actually run first: the chosen one, else a fallback.

    None means nothing in the chain can handle these files.
    """
    chain = (job.options.engine, *job.options.fallback_chain)
    return next((engine for engine in chain if can_run(job, engine)), None)


def routing_notice(job: SyncJob) -> str:
    """What a frontend should say before queueing, or "" if the chosen engine runs.

    Pair it with ``first_capable``: None there means this is a hard stop, not
    just a heads-up.
    """
    problem = capability_problem(job)
    if not problem:
        return ""
    engine = first_capable(job)
    if engine is None:
        return f"{problem}, and no fallback engine can either."
    return f"{problem}; {engines.get(engine).label} will run instead."


def validate(job: SyncJob) -> None:
    """Reject jobs that cannot possibly succeed, before spending a process."""
    if not job.reference_path.is_file():
        raise EngineError(f"Reference file is missing: {job.reference_path}")
    if not job.subtitle_path.is_file():
        raise EngineError(f"Subtitle file is missing: {job.subtitle_path}")
    if (
        not job.options.files.overwrite_input
        and job.output_path.exists()
        and not job.options.files.replace_output
    ):
        raise EngineError(f"Output already exists: {job.output_path}")
    if same_path(job.effective_output, job.reference_path):
        raise EngineError("Output would overwrite the reference file")
    if not job.options.files.overwrite_input and same_path(
        job.effective_output, job.subtitle_path
    ):
        raise EngineError("Output is the input subtitle; choose Overwrite explicitly")
    # The chosen engine being unable is not fatal while a fallback can step in.
    engine = first_capable(job)
    if engine is None:
        raise EngineError(capability_problem(job))
    spec = engines.get(engine)
    if spec.needs_ffmpeg and needs_ffmpeg(job.reference_path):
        status = probe(job.options.environment.ffmpeg_path)
        if not status.ready:
            raise EngineError(status.message)


def build_spec(
    jobs: Sequence[SyncJob],
    *,
    reference_override: Path | None = None,
    serialize_speech: bool = False,
    speech_output: Path | None = None,
    engine: EngineId | None = None,
) -> dict[str, Any]:
    """Describe a group of jobs sharing one reference for the worker.

    Every job must name the same reference. Options come from the first, since
    they are what the shared extraction is configured with. ``engine`` overrides
    the configured one, which is how a fallback chain retries the same jobs on a
    different tool without rewriting anything the user saved.
    """
    if not jobs:
        raise ValueError("a group needs at least one job")
    first = jobs[0]
    return {
        # The worker dispatches on this to pick an implementation module.
        "engine": (engine or first.options.engine).value,
        "reference": str(reference_override or first.reference_path),
        "options": first.options.to_dict(),
        "serialize_speech": serialize_speech,
        "speech_output": str(speech_output) if speech_output else None,
        # Every job in a group shares this; see queue._batch_key. None leaves
        # the choice to the worker's own rules.
        "reference_track": (
            first.reference_track.to_dict() if first.reference_track else None
        ),
        "items": [
            {
                "id": job.id,
                "subtitle": str(job.subtitle_path),
                "output": str(job.output_path),
                # Lets the embedded-track chooser prefer the same language.
                "language": job.language or "",
            }
            for job in jobs
        ],
    }


class SyncProcess:
    """One worker process handling one group, cancellable from any thread."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._engine = EngineId.parse(spec.get("engine"))
        self._job_ids = [str(item["id"]) for item in spec.get("items", [])]
        self._process: subprocess.Popen[str] | None = None
        # Held open after the spec is sent; closing it is the cancel signal.
        self._stdin: IO[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Ask the child to stop. Safe to call from another thread, or twice.

        Three escalating steps. Closing stdin is the graceful one: the worker
        sees EOF, stops between subtitles, reaps any native tool it spawned, and
        reports what it already finished. Terminate and kill follow for a worker
        that is stuck inside an alignment it cannot interrupt.
        """
        with self._lock:
            self._cancelled = True
            process = self._process
        self._close_stdin()
        if process is None or process.poll() is not None:
            return
        if self._wait_for(process, _CANCEL_GRACE):
            return
        try:
            process.terminate()
        except (OSError, ValueError):
            return  # Already reaped.
        if self._wait_for(process, _KILL_GRACE):
            return
        with contextlib.suppress(OSError, ValueError):
            process.kill()

    def _close_stdin(self) -> None:
        """Signal cancellation to the worker. Idempotent."""
        with self._lock:
            stream = self._stdin
            self._stdin = None
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    @staticmethod
    def _wait_for(process: subprocess.Popen[str], seconds: float) -> bool:
        """Poll for exit. Returns whether the process is gone."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return True
            time.sleep(0.05)
        return process.poll() is not None

    def _drain_stderr(self, stream: Any) -> None:
        with contextlib.suppress(OSError, ValueError):
            for line in stream:
                text = line.rstrip()
                if text:
                    self._stderr_tail.append(text)

    def run(
        self,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
    ) -> dict[str, SyncResult]:
        """Run the group. Returns one result per job id, always complete."""
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        environment.update(logger.child_environment())

        started = time.monotonic()
        results: dict[str, SyncResult] = {}

        with self._lock:
            if self._cancelled:
                return self._fill(results, JobStatus.CANCELLED, "Cancelled", started)
            try:
                process = subprocess.Popen(
                    worker_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    creationflags=creation_flags(),
                )
            except OSError as exc:
                return self._fill(
                    results,
                    JobStatus.FAILED,
                    f"Could not start sync worker: {exc}",
                    started,
                )
            self._process = process

        # tqdm writes a progress bar to stderr; if nobody drains it the pipe
        # buffer fills and the child blocks forever.
        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(process.stderr,), daemon=True
        )
        stderr_thread.start()

        try:
            if process.stdin is not None:
                # One line, and the pipe stays open: the worker reads exactly
                # this much, then treats EOF on the same pipe as "cancel".
                process.stdin.write(json.dumps(self._spec) + "\n")
                process.stdin.flush()
                with self._lock:
                    self._stdin = process.stdin
                if self._cancelled:
                    # Cancelled while we were starting up; the close above did
                    # nothing because stdin was not registered yet.
                    self._close_stdin()
        except (OSError, ValueError) as exc:
            self.cancel()
            return self._fill(
                results,
                JobStatus.FAILED,
                f"Could not send job to worker: {exc}",
                started,
            )

        fatal = ""
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            raw_id = event.get("job_id")
            job_id = str(raw_id) if raw_id is not None else None
            if kind == "progress" and on_progress is not None:
                fraction = event.get("fraction")
                on_progress(
                    job_id,
                    float(fraction) if isinstance(fraction, int | float) else None,
                    str(event.get("stage", "Working")),
                )
            elif kind == "log" and on_log is not None:
                on_log(
                    job_id,
                    str(event.get("level", "INFO")),
                    str(event.get("message", "")),
                )
            elif kind == "result" and job_id is not None:
                results[job_id] = _result_from(event, self._engine)
            elif kind == "fatal":
                fatal = str(event.get("error", ""))

        self._close_stdin()
        return_code = process.wait()
        stderr_thread.join(timeout=1.0)

        if self._cancelled:
            # Whatever finished before the kill keeps its result; the rest never
            # ran and are reported as cancelled rather than failed.
            return self._fill(results, JobStatus.CANCELLED, "Cancelled", started)

        if len(results) < len(self._job_ids):
            tail = " | ".join(list(self._stderr_tail)[-5:])
            detail = fatal or tail
            return self._fill(
                results,
                JobStatus.FAILED,
                (
                    f"Sync worker exited with code {return_code} without reporting a "
                    f"result{f': {detail}' if detail else ''}"
                ),
                started,
                return_code=return_code,
            )
        return results

    def _fill(
        self,
        results: dict[str, SyncResult],
        status: JobStatus,
        error: str,
        started: float,
        *,
        return_code: int = 1,
    ) -> dict[str, SyncResult]:
        """Give every job without a result the supplied outcome."""
        elapsed = time.monotonic() - started
        for job_id in self._job_ids:
            results.setdefault(
                job_id,
                SyncResult(
                    status=status,
                    return_code=return_code,
                    error=error,
                    duration_seconds=elapsed,
                    engine=self._engine,
                ),
            )
        return results


def _result_from(event: dict[str, Any], engine: EngineId) -> SyncResult:
    reasons = event.get("quality_reasons")
    return SyncResult(
        engine=engine,
        status=_STATUS_BY_NAME.get(
            str(event.get("status", "failed")), JobStatus.FAILED
        ),
        return_code=int(event.get("return_code", 0) or 0),
        offset_seconds=_optional_float(event.get("offset_seconds")),
        framerate_scale_factor=_optional_float(event.get("framerate_scale_factor")),
        score=_optional_float(event.get("score")),
        quality_reasons=[str(item) for item in reasons]
        if isinstance(reasons, list)
        else [],
        error=str(event.get("error", "")),
        duration_seconds=_optional_float(event.get("duration_seconds")),
        shared_extraction=bool(event.get("shared_extraction", False)),
    )


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
