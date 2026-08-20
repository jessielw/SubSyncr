"""Child process that runs exactly one ffsubsync job.

Run as ``python -m ffsubsyncui.core.worker``, or as ``<frozen exe> --sync-worker``
when packaged. Reads a JSON job spec on stdin and writes newline-delimited JSON
events on stdout.

Isolating ffsubsync in its own process buys three things that are impossible
in-process:

* **Cancellation.** ffsubsync exposes no cancel hook, but a process can be killed.
* **Clean logs at concurrency > 1.** The ``ffsubsync`` logger is process-global,
  so in-process jobs would interleave each other's output.
* **Root logger safety.** ``import ffsubsync`` calls ``logging.basicConfig()``,
  which would otherwise reconfigure the host application's logging.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, TextIO

from ffsubsyncui.core.scanner import needs_ffmpeg

# Log lines that carry results ffsubsync never returns to its caller.
_SCORE = re.compile(r"^score:\s*(-?\d+(?:\.\d+)?)")
_OFFSET = re.compile(r"^offset seconds:\s*(-?\d+(?:\.\d+)?)")
_SCALE = re.compile(r"^framerate scale factor:\s*(-?\d+(?:\.\d+)?)")
_LOW_QUALITY = re.compile(r"^low-quality alignment \((.*)\); leaving subtitles")


class EventChannel:
    """Newline-delimited JSON writer over a private copy of stdout."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: str, **payload: Any) -> None:
        payload["event"] = event
        try:
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            # The parent went away. Nothing useful left to do.
            pass


def claim_stdout() -> TextIO:
    """Take exclusive ownership of fd 1 for the event channel.

    Anything else that writes to stdout - a stray print, ffsubsync writing
    subtitles to ``sys.stdout.fileno()`` when ``-o`` is missing - is rerouted to
    stderr so it can never corrupt the protocol.

    The parent always supplies real pipes, but a windowed frozen build leaves
    ``sys.stdout``/``sys.stderr`` as ``None``, which breaks any library that
    writes to them (ffsubsync installs a Rich handler on stderr at import).
    Both are rebound to the descriptors we know are valid.
    """
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        diagnostics: TextIO = os.fdopen(
            2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False
        )
    except OSError:
        diagnostics = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = diagnostics
    sys.stderr = diagnostics
    return os.fdopen(saved_fd, "w", encoding="utf-8", errors="replace", buffering=1)


class _ResultScraper(logging.Handler):
    """Forward ffsubsync log records and mine them for hidden results."""

    def __init__(self, channel: EventChannel) -> None:
        super().__init__(logging.INFO)
        self._channel = channel
        self.score: float | None = None
        self.offset_seconds: float | None = None
        self.framerate_scale_factor: float | None = None
        self.low_quality_reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        if match := _SCORE.match(message):
            self.score = float(match[1])
        elif match := _OFFSET.match(message):
            self.offset_seconds = float(match[1])
        elif match := _SCALE.match(message):
            self.framerate_scale_factor = float(match[1])
        elif match := _LOW_QUALITY.match(message):
            self.low_quality_reasons = [
                reason.strip() for reason in match[1].split(";") if reason.strip()
            ]
        self._channel.emit("log", level=record.levelname, message=message)


def build_arguments(spec: dict[str, Any]) -> list[str]:
    """Translate a job spec into an ffsubsync argv."""
    options: dict[str, Any] = spec.get("options") or {}
    reference = str(spec["reference"])
    arguments = [reference, "-i", str(spec["subtitle"])]

    if options.get("overwrite_input"):
        arguments.append("--overwrite-input")
    else:
        # Always explicit. With no -o, ffsubsync writes subtitle bytes straight
        # to stdout, and validate_args calls sys.stdin.isatty() when -i is absent.
        arguments.extend(("-o", str(spec["output"])))

    arguments.extend(("--encoding", str(options.get("encoding", "infer"))))
    arguments.extend(
        ("--output-encoding", str(options.get("output_encoding", "utf-8")))
    )
    arguments.extend(
        ("--max-offset-seconds", str(options.get("max_offset_seconds", 60.0)))
    )
    arguments.extend(("--start-seconds", str(int(options.get("start_seconds", 0)))))
    arguments.extend(("--frame-rate", str(int(options.get("frame_rate", 48000)))))
    arguments.extend(("--non-speech-label", str(options.get("non_speech_label", 0.0))))
    arguments.extend(
        ("--apply-offset-seconds", str(options.get("apply_offset_seconds", 0.0)))
    )

    # VAD only applies when speech has to be detected in audio. Subtitle and
    # serialized-speech references warn ("Vad specified, but reference was not
    # a movie") and ignore it.
    if needs_ffmpeg(Path(reference)) and options.get("vad"):
        arguments.extend(("--vad", str(options["vad"])))

    if options.get("no_fix_framerate"):
        arguments.append("--no-fix-framerate")
    if options.get("gss"):
        arguments.append("--gss")
    if stream := str(options.get("reference_stream", "")).strip():
        arguments.extend(("--reference-stream", stream))
    if ffmpeg_path := str(options.get("ffmpeg_path", "")).strip():
        arguments.extend(("--ffmpeg-path", ffmpeg_path))

    if (split_penalty := options.get("split_penalty")) is not None:
        arguments.extend(("--split-penalty", str(split_penalty)))
    if options.get("multi_segment_sync"):
        arguments.append("--multi-segment-sync")
        arguments.extend(("--segment-count", str(int(options.get("segment_count", 8)))))
    if (max_duration := options.get("max_duration_seconds")) is not None:
        arguments.extend(("--max-duration-seconds", str(max_duration)))

    if options.get("skip_sync_on_low_quality"):
        arguments.append("--skip-sync-on-low-quality")
        arguments.extend(("--min-score", str(options.get("min_score", 0.0))))
        arguments.extend(
            (
                "--quality-max-offset-seconds",
                str(options.get("quality_max_offset_seconds", 30.0)),
            )
        )
        arguments.extend(
            (
                "--max-framerate-deviation",
                str(options.get("max_framerate_deviation", 0.1)),
            )
        )

    if spec.get("serialize_speech"):
        arguments.append("--serialize-speech")

    return arguments


def run_job(spec: dict[str, Any], channel: EventChannel) -> int:
    """Execute one sync. Returns a process exit code."""
    # Imported here so claim_stdout() runs first and the basicConfig side effect
    # of this import lands in a process we own.
    import ffsubsync
    from ffsubsync.ffsubsync import make_parser

    started = time.monotonic()
    scraper = _ResultScraper(channel)
    logger = logging.getLogger("ffsubsync")
    logger.addHandler(scraper)

    overwrite = bool((spec.get("options") or {}).get("overwrite_input"))
    expected_output = Path(str(spec["subtitle"] if overwrite else spec["output"]))

    try:
        arguments = build_arguments(spec)
        channel.emit("started", pid=os.getpid(), argv=arguments)

        last_fraction = -1.0

        def on_progress(info: Any) -> None:
            nonlocal last_fraction
            raw = getattr(info, "fraction", None)
            fraction = float(raw) if raw is not None else None
            if fraction is not None and abs(fraction - last_fraction) < 0.005:
                return  # Don't flood the pipe with sub-percent updates.
            if fraction is not None:
                last_fraction = fraction
            stage = (
                "Aligning"
                if fraction is not None and fraction >= 0.999
                else "Extracting speech"
            )
            channel.emit("progress", fraction=fraction, stage=stage)

        namespace = make_parser().parse_args(arguments)
        raw_result: Any = ffsubsync.run(namespace, on_progress)
        result = raw_result if isinstance(raw_result, dict) else {}

        return_code = int(result.get("retval", 1) or 0)
        # retval stays 0 even when the alignment fails, so it is never
        # sufficient on its own.
        reported_success = bool(result.get("sync_was_successful", False))
        output_exists = expected_output.is_file()

        if scraper.low_quality_reasons:
            # ffsubsync wrote the *original* subtitles to the output path, so
            # the file existing proves nothing here.
            status = "skipped_low_quality"
            error = "; ".join(scraper.low_quality_reasons)
        elif return_code == 0 and reported_success and output_exists:
            status = "succeeded"
            error = ""
        else:
            status = "failed"
            if not output_exists and return_code == 0 and reported_success:
                error = (
                    "ffsubsync reported success but wrote no output to "
                    f"{expected_output}"
                )
            elif return_code != 0:
                error = f"ffsubsync exited with code {return_code}"
            else:
                error = "ffsubsync could not align these files"

        channel.emit(
            "result",
            status=status,
            return_code=return_code,
            offset_seconds=(
                _as_float(result.get("offset_seconds"))
                if result.get("offset_seconds") is not None
                else scraper.offset_seconds
            ),
            framerate_scale_factor=(
                _as_float(result.get("framerate_scale_factor"))
                if result.get("framerate_scale_factor") is not None
                else scraper.framerate_scale_factor
            ),
            score=scraper.score,
            quality_reasons=scraper.low_quality_reasons,
            error=error,
            duration_seconds=time.monotonic() - started,
        )
        return 0
    except Exception as exc:
        channel.emit(
            "result",
            status="failed",
            return_code=1,
            error=str(exc) or type(exc).__name__,
            duration_seconds=time.monotonic() - started,
        )
        return 1
    finally:
        logger.removeHandler(scraper)
        scraper.close()


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def main(argv: list[str] | None = None) -> int:
    del argv  # The spec arrives on stdin, not the command line.
    channel = EventChannel(claim_stdout())
    try:
        spec = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError) as exc:
        channel.emit(
            "result", status="failed", return_code=2, error=f"Bad job spec: {exc}"
        )
        return 2
    if not isinstance(spec, dict):
        channel.emit(
            "result", status="failed", return_code=2, error="Job spec must be an object"
        )
        return 2
    return run_job(spec, channel)


if __name__ == "__main__":
    raise SystemExit(main())
