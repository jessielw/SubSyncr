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
from typing import Any

from ffsubsyncui.core.ffmpeg import probe
from ffsubsyncui.core.models import JobStatus, SyncJob, SyncResult
from ffsubsyncui.core.scanner import needs_ffmpeg

# job_id (None for group-wide events), fraction, stage
ProgressCallback = Callable[[str | None, float | None, str], None]
# job_id (None for group-wide events), level, message
LogCallback = Callable[[str | None, str, str], None]

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
        # The packaged executable re-execs itself; see ffsubsyncui.__main__.
        return [sys.executable, "--sync-worker"]
    return [sys.executable, "-m", "ffsubsyncui.core.worker"]


def _creation_flags() -> int:
    # Without this the frozen GUI flashes a console window per job.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def validate(job: SyncJob) -> None:
    """Reject jobs that cannot possibly succeed, before spending a process."""
    if not job.reference_path.is_file():
        raise EngineError(f"Reference file is missing: {job.reference_path}")
    if not job.subtitle_path.is_file():
        raise EngineError(f"Subtitle file is missing: {job.subtitle_path}")
    if (
        not job.options.overwrite_input
        and job.output_path.exists()
        and not job.options.replace_output
    ):
        raise EngineError(f"Output already exists: {job.output_path}")
    if needs_ffmpeg(job.reference_path):
        status = probe(job.options.ffmpeg_path)
        if not status.ready:
            raise EngineError(status.message)


def build_spec(
    jobs: Sequence[SyncJob],
    *,
    reference_override: Path | None = None,
    serialize_speech: bool = False,
) -> dict[str, Any]:
    """Describe a group of jobs sharing one reference for the worker.

    Every job must name the same reference. Options come from the first, since
    they are what the shared extraction is configured with.
    """
    if not jobs:
        raise ValueError("a group needs at least one job")
    first = jobs[0]
    return {
        "reference": str(reference_override or first.reference_path),
        "options": first.options.to_dict(),
        "serialize_speech": serialize_speech,
        "items": [
            {
                "id": job.id,
                "subtitle": str(job.subtitle_path),
                "output": str(job.output_path),
            }
            for job in jobs
        ],
    }


class SyncProcess:
    """One worker process handling one group, cancellable from any thread."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._job_ids = [str(item["id"]) for item in spec.get("items", [])]
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Ask the child to stop. Safe to call from another thread, or twice."""
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except (OSError, ValueError):
            return  # Already reaped.
        # Give it a moment to unwind, then stop asking.
        deadline = time.monotonic() + _KILL_GRACE
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.05)
        with contextlib.suppress(OSError, ValueError):
            process.kill()

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
                    creationflags=_creation_flags(),
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
                process.stdin.write(json.dumps(self._spec))
                process.stdin.close()
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
                results[job_id] = _result_from(event)
            elif kind == "fatal":
                fatal = str(event.get("error", ""))

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
                ),
            )
        return results


def _result_from(event: dict[str, Any]) -> SyncResult:
    reasons = event.get("quality_reasons")
    return SyncResult(
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
