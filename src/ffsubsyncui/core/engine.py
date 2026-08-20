"""Parent side of the sync subprocess: spawn, stream events, cancel."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
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

ProgressCallback = Callable[[float | None, str], None]
LogCallback = Callable[[str, str], None]

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
    job: SyncJob,
    *,
    reference_override: Path | None = None,
    serialize_speech: bool = False,
) -> dict[str, Any]:
    return {
        "reference": str(reference_override or job.reference_path),
        "subtitle": str(job.subtitle_path),
        "output": str(job.output_path),
        "options": job.options.to_dict(),
        "serialize_speech": serialize_speech,
    }


class SyncProcess:
    """One ffsubsync run, owned by one worker thread but cancellable from any."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
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
    ) -> SyncResult:
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"

        started = time.monotonic()
        with self._lock:
            if self._cancelled:
                return SyncResult(status=JobStatus.CANCELLED, error="Cancelled")
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
                return SyncResult(
                    status=JobStatus.FAILED,
                    return_code=1,
                    error=f"Could not start sync worker: {exc}",
                )
            self._process = process

        # tqdm writes a progress bar to stderr; if nobody drains it the pipe
        # buffer fills and the child blocks forever.
        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(process.stderr,), daemon=True
        )
        stderr_thread.start()

        payload: dict[str, Any] | None = None
        try:
            if process.stdin is not None:
                process.stdin.write(json.dumps(self._spec))
                process.stdin.close()
        except (OSError, ValueError) as exc:
            self.cancel()
            return SyncResult(
                status=JobStatus.FAILED,
                return_code=1,
                error=f"Could not send job to worker: {exc}",
            )

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
            if kind == "progress" and on_progress is not None:
                fraction = event.get("fraction")
                on_progress(
                    float(fraction) if isinstance(fraction, int | float) else None,
                    str(event.get("stage", "Working")),
                )
            elif kind == "log" and on_log is not None:
                on_log(str(event.get("level", "INFO")), str(event.get("message", "")))
            elif kind == "result":
                payload = event

        return_code = process.wait()
        stderr_thread.join(timeout=1.0)

        if self._cancelled:
            return SyncResult(
                status=JobStatus.CANCELLED,
                return_code=return_code,
                error="Cancelled",
                duration_seconds=time.monotonic() - started,
            )

        if payload is None:
            tail = " | ".join(list(self._stderr_tail)[-5:])
            return SyncResult(
                status=JobStatus.FAILED,
                return_code=return_code,
                error=(
                    f"Sync worker exited with code {return_code} without reporting a "
                    f"result{f': {tail}' if tail else ''}"
                ),
                duration_seconds=time.monotonic() - started,
            )

        status = _STATUS_BY_NAME.get(
            str(payload.get("status", "failed")), JobStatus.FAILED
        )
        reasons = payload.get("quality_reasons")
        return SyncResult(
            status=status,
            return_code=int(payload.get("return_code", return_code) or 0),
            offset_seconds=_optional_float(payload.get("offset_seconds")),
            framerate_scale_factor=_optional_float(
                payload.get("framerate_scale_factor")
            ),
            score=_optional_float(payload.get("score")),
            quality_reasons=[str(item) for item in reasons]
            if isinstance(reasons, list)
            else [],
            error=str(payload.get("error", "")),
            duration_seconds=_optional_float(payload.get("duration_seconds"))
            or (time.monotonic() - started),
        )


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
