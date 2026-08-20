from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
import os
from pathlib import Path
import shutil
from typing import Any, cast

import ffsubsync
from ffsubsync.ffsubsync import make_parser

from ffsubsyncui.domain import SyncJob, SyncResult

ProgressCallback = Callable[[float | None, str], None]
LogCallback = Callable[[str], None]
_SUBTITLE_REFERENCE_SUFFIXES = {".ass", ".srt", ".ssa", ".sub"}


class RunnerError(RuntimeError):
    """Raised when a job cannot safely be started."""


class _CallbackHandler(logging.Handler):
    def __init__(self, callback: LogCallback) -> None:
        super().__init__(logging.INFO)
        self._callback = callback
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:
            self.handleError(record)


def _tool_names() -> tuple[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    return f"ffmpeg{suffix}", f"ffprobe{suffix}"


def ffmpeg_status(configured_path: str = "") -> tuple[bool, str]:
    ffmpeg_name, ffprobe_name = _tool_names()
    if configured_path.strip():
        candidate = Path(configured_path).expanduser()
        directory = candidate if candidate.is_dir() else candidate.parent
        ffmpeg = directory / ffmpeg_name
        ffprobe = directory / ffprobe_name
        if candidate.is_file() and candidate.name.casefold() == ffmpeg_name.casefold():
            ffmpeg = candidate
        if ffmpeg.is_file() and ffprobe.is_file():
            return True, f"FFmpeg ready: {directory}"
        return False, f"FFmpeg or ffprobe is missing from {directory}"
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return True, f"FFmpeg ready: {Path(ffmpeg).parent}"
    return False, "FFmpeg and ffprobe were not found on PATH"


class FFSubsyncRunner:
    def build_arguments(self, job: SyncJob) -> list[str]:
        options = job.options
        arguments = [str(job.reference_path), "-i", str(job.subtitle_path)]
        if options.overwrite_input:
            arguments.append("--overwrite-input")
        else:
            arguments.extend(("-o", str(job.output_path)))
        arguments.extend(("--encoding", options.encoding))
        arguments.extend(("--output-encoding", options.output_encoding))
        arguments.extend(("--max-offset-seconds", str(options.max_offset_seconds)))
        if options.vad:
            arguments.extend(("--vad", options.vad))
        if options.no_fix_framerate:
            arguments.append("--no-fix-framerate")
        if options.quality_gate:
            arguments.append("--skip-sync-on-low-quality")
        if options.reference_stream.strip():
            arguments.extend(("--reference-stream", options.reference_stream.strip()))
        if options.ffmpeg_path.strip():
            arguments.extend(("--ffmpeg-path", options.ffmpeg_path.strip()))
        arguments.append("--gui-mode")
        return arguments

    def validate(self, job: SyncJob) -> None:
        if not job.reference_path.is_file():
            raise RunnerError(f"Reference file is missing: {job.reference_path}")
        if not job.subtitle_path.is_file():
            raise RunnerError(f"Subtitle file is missing: {job.subtitle_path}")
        if (
            not job.options.overwrite_input
            and job.output_path.exists()
            and not job.options.replace_output
        ):
            raise RunnerError(f"Output already exists: {job.output_path}")
        if job.reference_path.suffix.casefold() not in _SUBTITLE_REFERENCE_SUFFIXES:
            ready, message = ffmpeg_status(job.options.ffmpeg_path)
            if not ready:
                raise RunnerError(message)

    def run(
        self,
        job: SyncJob,
        *,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
    ) -> SyncResult:
        self.validate(job)
        logger = logging.getLogger("ffsubsync")
        handler = _CallbackHandler(log_callback)
        logger.addHandler(handler)
        try:
            progress_callback(None, "Starting")

            def on_progress(info: Any) -> None:
                fraction = getattr(info, "fraction", None)
                value = float(fraction) if fraction is not None else None
                stage = (
                    "Aligning and writing"
                    if value is not None and value >= 0.999
                    else "Extracting audio"
                )
                progress_callback(value, stage)

            namespace = make_parser().parse_args(self.build_arguments(job))
            raw = cast(Mapping[str, object], ffsubsync.run(namespace, on_progress))
            return_code = int(cast(int, raw.get("retval", 1)))
            successful = bool(raw.get("sync_was_successful", False))
            expected_output = (
                job.subtitle_path if job.options.overwrite_input else job.output_path
            )
            success = return_code == 0 and successful and expected_output.is_file()
            error = "" if success else "ffsubsync did not produce a successful result"
            return SyncResult(
                success=success,
                return_code=return_code,
                offset_seconds=_optional_float(raw.get("offset_seconds")),
                framerate_scale_factor=_optional_float(
                    raw.get("framerate_scale_factor")
                ),
                error=error,
            )
        except RunnerError:
            raise
        except Exception as exc:
            raise RunnerError(str(exc) or type(exc).__name__) from exc
        finally:
            logger.removeHandler(handler)
            handler.close()


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
